from __future__ import annotations

import hashlib
import json
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Literal

from app.channels.douyin.config import DouyinAccount


class DouyinBrowserError(RuntimeError):
    """The browser is unavailable, unverified, or the private-message UI changed."""


@dataclass(frozen=True)
class DouyinInboundEvent:
    message_key: str
    conversation_id: str
    sender_id: str
    sender_name: str
    text: str
    timestamp: int
    target: dict
    kind: str = "text"
    media_url: str = ""
    title: str = ""
    description: str = ""
    url: str = ""
    display_time: str = ""
    sender_avatar: str = ""
    initial_scan: bool = False


@dataclass(frozen=True)
class DouyinDeliveryReceipt:
    client_message_id: str
    status: Literal["confirmed", "failed", "unknown"]
    error: str = ""


@dataclass(frozen=True)
class DouyinConversation:
    conversation_id: str
    name: str
    preview: str
    target: dict
    avatar_url: str = ""


@dataclass(frozen=True)
class DouyinConversationHistory:
    conversation_id: str
    messages: list[dict]


@dataclass
class _Command:
    operation: str
    args: tuple = ()
    deadline: float = float("inf")
    client_message_id: str = ""
    event: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)
    cancelled: bool = False
    side_effect_started: bool = False
    result: object | None = None
    error: BaseException | None = None


_SCAN_LIST_SCRIPT = r"""
() => {
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 8 && r.height > 8 && r.right > 0 && r.left < innerWidth &&
      r.bottom > 0 && r.top < innerHeight && s.display !== 'none' &&
      s.visibility !== 'hidden' && Number.parseFloat(s.opacity || '1') > 0.05;
  };
  const topmost = (el) => {
    const r = el.getBoundingClientRect();
    const left = Math.max(0, r.left);
    const right = Math.min(innerWidth, r.right);
    const top = Math.max(0, r.top);
    const bottom = Math.min(innerHeight, r.bottom);
    if (right - left < 2 || bottom - top < 2) return false;
    const hit = document.elementFromPoint(
      (left + right) / 2,
      (top + bottom) / 2
    );
    return Boolean(hit && el.contains(hit));
  };
  const red = (el) => {
    const s = getComputedStyle(el);
    const bg = String(s.backgroundColor || '').replace(/\s/g, '');
    const text = String(el.innerText || el.textContent || '').trim();
    const r = el.getBoundingClientRect();
    const redBg = bg.includes('254,44,85') || bg.includes('255,44,85');
    return visible(el) && redBg && r.width <= 36 && r.height <= 36 &&
      (/^\d+$/.test(text) || !text);
  };
  document.querySelectorAll('[data-acs-douyin-active-index]').forEach(el => {
    el.removeAttribute('data-acs-douyin-active-index');
  });
  let items = Array.from(document.querySelectorAll('[data-e2e="conversation-item"]'));
  const currentLayout = items.length > 0;
  if (!items.length) items = Array.from(document.querySelectorAll('[role="listitem"]'));
  if (!items.length) items = Array.from(document.querySelectorAll('div[data-uid],div[data-user-id]'));
  return items.filter(item => {
    if (!visible(item) || !topmost(item)) return false;
    const r = item.getBoundingClientRect();
    if (currentLayout) {
      return r.left >= innerWidth - 460 && r.width >= 180 && r.width <= 440 &&
        r.height >= 44 && r.height <= 120;
    }
    return r.left < 620 && r.width >= 120 && r.width <= 520 &&
      r.height >= 32 && r.height <= 150;
  }).map((item, index) => {
    item.setAttribute('data-acs-douyin-active-index', String(index));
    const lines = String(item.innerText || '').split('\n').map(v => v.trim()).filter(Boolean);
    const nameNode = item.querySelector(
      '[class*="conversationConversationItemtitle"]:not([class*="Wrapper"])'
    );
    const previewNode = item.querySelector('pre[class*="ConversationItemHint"],[class*="ConversationItemHinttextBox"]');
    const unreadNode = Array.from(item.querySelectorAll('span,div')).find(red);
    const uidNode = item.matches('[data-uid],[data-user-id]')
      ? item : item.querySelector('[data-uid],[data-user-id]');
    const uid = uidNode?.getAttribute('data-uid') || uidNode?.getAttribute('data-user-id') || '';
    const name = String(nameNode?.innerText || nameNode?.textContent || lines[0] || uid || '').trim();
    const preview = String(previewNode?.innerText || previewNode?.textContent || lines.slice(1).join(' ') || '').trim();
    let avatarPath = '';
    let avatarRemote = '';
    const avatarUrls = Array.from(item.querySelectorAll('img')).flatMap(avatar => {
      const srcset = String(avatar?.getAttribute('srcset') || '').split(',')[0].trim().split(/\s+/)[0];
      return [
        avatar?.getAttribute('src'),
        avatar?.currentSrc,
        avatar?.getAttribute('data-src'),
        srcset,
      ].filter(Boolean);
    });
    for (const avatarUrl of avatarUrls) {
      try {
        const parsedAvatar = new URL(avatarUrl, location.origin);
        if (!avatarPath) avatarPath = parsedAvatar.pathname;
        const host = parsedAvatar.hostname.toLowerCase();
        const allowed = ['douyinpic.com', 'byteimg.com', 'ibytedtos.com'].some(suffix =>
          host === suffix || host.endsWith('.' + suffix)
        );
        avatarRemote = parsedAvatar.protocol === 'https:' && allowed ? parsedAvatar.href : '';
        if (avatarRemote) break;
      } catch (_error) {
        if (!avatarPath) avatarPath = avatarUrl.split('?')[0];
      }
    }
    return {
      uid: String(uid || ''),
      identity_seed: uid ? '' : String(name + '\n' + avatarPath),
      locator_kind: uid ? 'stable_uid' : 'conversation_item',
      index,
      name,
      preview,
      avatar_url: avatarRemote,
      unread: Boolean(unreadNode),
    };
  }).filter(row => row.name && (row.uid || row.identity_seed));
}
"""

_OPEN_INBOX_SCRIPT = r"""
() => {
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 8 && r.height > 8 && s.display !== 'none' && s.visibility !== 'hidden';
  };
  const drawerTitle = Array.from(document.querySelectorAll('div,span,h1,h2,h3')).find(el => {
    if (!visible(el)) return false;
    const r = el.getBoundingClientRect();
    const text = String(el.innerText || el.textContent || '').trim();
    return text === '消息' && r.left >= innerWidth - 430 && r.top >= 70 &&
      r.top <= 220 && r.width <= 160 && r.height <= 80;
  });
  if (drawerTitle) return 'open';
  const imEntry = document.querySelector('[data-e2e="im-entry"]');
  if (imEntry && visible(imEntry)) {
    document.querySelectorAll('[data-acs-douyin-inbox-target]').forEach(el => {
      el.removeAttribute('data-acs-douyin-inbox-target');
    });
    imEntry.setAttribute('data-acs-douyin-inbox-target', 'true');
    return 'target';
  }
  const candidates = Array.from(document.querySelectorAll(
    'a,button,[role="button"],[role="tab"],div,span'
  )).filter(el => {
    if (!visible(el)) return false;
    const text = String(el.innerText || el.textContent || '').trim();
    if (text !== '消息' && text !== '私信') return false;
    const r = el.getBoundingClientRect();
    return r.top >= 0 && r.top <= 220 && r.width <= 240 && r.height <= 100;
  }).map(el => el.closest('a,button,[role="button"],[role="tab"],li,[data-e2e]') || el)
    .filter((el, index, rows) => rows.indexOf(el) === index);
  candidates.sort((left, right) => {
    const score = (el) => {
      const tag = String(el.tagName || '').toLowerCase();
      const role = String(el.getAttribute('role') || '');
      return Number(tag === 'a' || tag === 'button' || tag === 'li' ||
        role === 'button' || role === 'tab' || el.hasAttribute('data-e2e'));
    };
    const scoreDelta = score(right) - score(left);
    if (scoreDelta) return scoreDelta;
    const leftRect = left.getBoundingClientRect();
    const rightRect = right.getBoundingClientRect();
    return (leftRect.width * leftRect.height) - (rightRect.width * rightRect.height);
  });
  if (!candidates.length) return false;
  document.querySelectorAll('[data-acs-douyin-inbox-target]').forEach(el => {
    el.removeAttribute('data-acs-douyin-inbox-target');
  });
  candidates[0].setAttribute('data-acs-douyin-inbox-target', 'true');
  return 'target';
}
"""

_MAIN_INBOX_VISIBLE_SCRIPT = r"""
() => {
  const topmost = el => {
    const r = el.getBoundingClientRect();
    if (r.width < 8 || r.height < 8) return false;
    const style = getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden' ||
        Number.parseFloat(style.opacity || '1') <= 0.05) return false;
    const left = Math.max(0, r.left);
    const right = Math.min(innerWidth, r.right);
    const top = Math.max(0, r.top);
    const bottom = Math.min(innerHeight, r.bottom);
    if (right - left < 2 || bottom - top < 2) return false;
    const x = (left + right) / 2;
    const y = (top + bottom) / 2;
    const hit = document.elementFromPoint(x, y);
    return Boolean(hit && el.contains(hit));
  };
  const strangerHeaders = Array.from(document.querySelectorAll(
    '[class*="conversationStrangerConversationListhead"]'
  ));
  if (strangerHeaders.some(topmost)) return false;
  const entries = Array.from(document.querySelectorAll(
    '[class*="conversationStrangerBoxwrapper"]'
  ));
  if (entries.some(topmost)) return true;
  return Array.from(document.querySelectorAll('[data-e2e="conversation-item"]'))
    .some(topmost);
}
"""

_CLICK_VISIBLE_BACK_SCRIPT = r"""
() => {
  document.querySelectorAll('[data-acs-douyin-visible-back]').forEach(el => {
    el.removeAttribute('data-acs-douyin-visible-back');
  });
  const groups = [
    Array.from(document.querySelectorAll(
      '[class*="conversationStrangerConversationListhead"] '
      + '[class*="conversationStrangerConversationListicon"], '
      + '[class*="conversationStrangerConversationListhead"] svg'
    )),
    Array.from(document.querySelectorAll(
      'svg[class*="StackLayoutStackTitleBarbackBtn"]'
    )),
  ];
  const topmost = el => {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    const style = getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden' ||
        Number.parseFloat(style.opacity || '1') <= 0.05) return false;
    const left = Math.max(0, r.left);
    const right = Math.min(innerWidth, r.right);
    const top = Math.max(0, r.top);
    const bottom = Math.min(innerHeight, r.bottom);
    if (right - left < 2 || bottom - top < 2) return false;
    const hit = document.elementFromPoint((left + right) / 2, (top + bottom) / 2);
    return Boolean(hit && el.contains(hit));
  };
  const button = groups.map(group => group.find(topmost)).find(Boolean);
  if (!button) return false;
  button.setAttribute('data-acs-douyin-visible-back', 'true');
  return true;
}
"""

_OPEN_STRANGER_LIST_SCRIPT = r"""
() => {
  document.querySelectorAll('[data-acs-douyin-stranger-entry]').forEach(el => {
    el.removeAttribute('data-acs-douyin-stranger-entry');
  });
  const entries = Array.from(document.querySelectorAll(
    '[class*="conversationStrangerBoxwrapper"]'
  ));
  const entry = entries.find(el => {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    const style = getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden' ||
        Number.parseFloat(style.opacity || '1') <= 0.05) return false;
    const left = Math.max(0, r.left);
    const right = Math.min(innerWidth, r.right);
    const top = Math.max(0, r.top);
    const bottom = Math.min(innerHeight, r.bottom);
    if (right - left < 2 || bottom - top < 2) return false;
    const hit = document.elementFromPoint((left + right) / 2, (top + bottom) / 2);
    return Boolean(hit && el.contains(hit));
  });
  if (!entry) return false;
  entry.setAttribute('data-acs-douyin-stranger-entry', 'true');
  return true;
}
"""

_STRANGER_LIST_VISIBLE_SCRIPT = r"""
() => Array.from(document.querySelectorAll(
  '[class*="conversationStrangerConversationListhead"]'
)).some(el => {
  const r = el.getBoundingClientRect();
  if (r.width < 8 || r.height < 8) return false;
  const style = getComputedStyle(el);
  if (style.display === 'none' || style.visibility === 'hidden' ||
      Number.parseFloat(style.opacity || '1') <= 0.05) return false;
  const left = Math.max(0, r.left);
  const right = Math.min(innerWidth, r.right);
  const top = Math.max(0, r.top);
  const bottom = Math.min(innerHeight, r.bottom);
  if (right - left < 2 || bottom - top < 2) return false;
  const x = (left + right) / 2;
  const y = (top + bottom) / 2;
  const hit = document.elementFromPoint(x, y);
  return Boolean(hit && el.contains(hit));
})
"""

_SCROLL_INBOX_TO_TOP_SCRIPT = r"""
() => {
  const topmost = el => {
    const r = el.getBoundingClientRect();
    if (r.width < 8 || r.height < 8) return false;
    const style = getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden' ||
        Number.parseFloat(style.opacity || '1') <= 0.05) return false;
    const left = Math.max(0, r.left);
    const right = Math.min(innerWidth, r.right);
    const top = Math.max(0, r.top);
    const bottom = Math.min(innerHeight, r.bottom);
    if (right - left < 2 || bottom - top < 2) return false;
    const x = (left + right) / 2;
    const y = (top + bottom) / 2;
    const hit = document.elementFromPoint(x, y);
    return Boolean(hit && el.contains(hit));
  };
  const row = Array.from(document.querySelectorAll('[data-e2e="conversation-item"]'))
    .find(topmost);
  if (!row) return false;
  for (let parent = row.parentElement; parent; parent = parent.parentElement) {
    if (parent.scrollHeight > parent.clientHeight + 4) {
      parent.scrollTop = 0;
      parent.dispatchEvent(new Event('scroll', {bubbles: true}));
      return true;
    }
  }
  return false;
}
"""

_SCROLL_ACTIVE_CONVERSATION_LIST_SCRIPT = r"""
(action) => {
  const topmost = el => {
    const r = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    if (r.width < 8 || r.height < 8 || r.right <= 0 || r.left >= innerWidth ||
        r.bottom <= 0 || r.top >= innerHeight || style.display === 'none' ||
        style.visibility === 'hidden' || Number.parseFloat(style.opacity || '1') <= 0.05) {
      return false;
    }
    const left = Math.max(0, r.left);
    const right = Math.min(innerWidth, r.right);
    const top = Math.max(0, r.top);
    const bottom = Math.min(innerHeight, r.bottom);
    const hit = document.elementFromPoint((left + right) / 2, (top + bottom) / 2);
    return Boolean(hit && el.contains(hit));
  };
  const row = Array.from(document.querySelectorAll('[data-e2e="conversation-item"]'))
    .find(topmost);
  if (!row) return {found: false, moved: false, at_end: true};
  let scroller = null;
  for (let parent = row.parentElement; parent; parent = parent.parentElement) {
    if (parent.scrollHeight > parent.clientHeight + 4) {
      scroller = parent;
      break;
    }
  }
  if (!scroller) return {found: false, moved: false, at_end: true};
  const before = Number(scroller.scrollTop || 0);
  const maximum = Math.max(0, scroller.scrollHeight - scroller.clientHeight);
  const desired = action === 'top'
    ? 0
    : Math.min(maximum, before + Math.max(120, scroller.clientHeight * 0.75));
  scroller.scrollTop = desired;
  scroller.dispatchEvent(new Event('scroll', {bubbles: true}));
  const after = Number(scroller.scrollTop || 0);
  return {
    found: true,
    moved: Math.abs(after - before) > 1,
    at_end: after >= maximum - 1,
  };
}
"""

_READ_CHAT_SCRIPT = r"""
() => {
  const rendered = (el) => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 8 && r.height > 8 &&
      s.display !== 'none' && s.visibility !== 'hidden';
  };
  const topmost = (el) => {
    const r = el.getBoundingClientRect();
    const x = Math.max(0, Math.min(innerWidth - 1, r.left + r.width / 2));
    const y = Math.max(0, Math.min(innerHeight - 1, r.top + r.height / 2));
    const hit = document.elementFromPoint(x, y);
    return Boolean(hit && (el.contains(hit) || hit.contains(el)));
  };
  const safeImageUrl = (value) => {
    try {
      const parsed = new URL(String(value || ''), location.origin);
      const host = parsed.hostname.toLowerCase();
      const allowed = [
        'douyinpic.com', 'byteimg.com', 'douyinvod.com', 'ibytedtos.com',
      ].some(suffix => host === suffix || host.endsWith('.' + suffix));
      return parsed.protocol === 'https:' && allowed ? parsed.href : '';
    } catch (_error) {
      return '';
    }
  };
  const imageUrl = (el) => safeImageUrl(
    el?.currentSrc || el?.getAttribute?.('src') || el?.getAttribute?.('data-src') || ''
  );
  const reactMessageId = (el) => {
    const internalKey = Object.keys(el || {}).find(key =>
      key.startsWith('__reactFiber$') || key.startsWith('__reactInternalInstance$')
    );
    let fiber = internalKey ? el[internalKey] : null;
    let fallback = '';
    for (let depth = 0; fiber && depth < 14; depth += 1, fiber = fiber.return) {
      const props = fiber.memoizedProps;
      const virtualId = String(props?.virtualItem?.id || '');
      if (virtualId) return virtualId;
      const fiberKey = String(fiber.key || '');
      if (!fallback && /^[A-Za-z0-9-]{16,}$/.test(fiberKey)) {
        fallback = fiberKey;
      }
    }
    return fallback;
  };
  const reactCardUrl = (el) => {
    const internalKey = Object.keys(el || {}).find(key =>
      key.startsWith('__reactFiber$') || key.startsWith('__reactInternalInstance$')
    );
    let fiber = internalKey ? el[internalKey] : null;
    const urlFromParsedContent = (parsed) => {
      if (!parsed || typeof parsed !== 'object') return '';
      const schema = String(parsed.schema || '');
      const schemaMatch = schema.match(/aweme:\/\/aweme\/detail\/(\d{15,24})/);
      const itemId = String(
        parsed.itemId || parsed.item_id || parsed.aweme_info?.item_id ||
        parsed.related_share_video?.itemId || schemaMatch?.[1] || ''
      );
      return /^\d{15,24}$/.test(itemId)
        ? 'https://www.douyin.com/video/' + itemId : '';
    };
    for (let depth = 0; fiber && depth < 14; depth += 1, fiber = fiber.return) {
      const props = fiber.memoizedProps;
      const parsedCandidates = [
        props?.message?.parsedContent,
        props?.children?.props?.message?.parsedContent,
        props?.refMessageConfig?.message?.parsedContent,
        props?.children?.props?.refMessageConfig?.message?.parsedContent,
      ];
      for (const parsed of parsedCandidates) {
        const url = urlFromParsedContent(parsed);
        if (url) return url;
      }
    }
    return '';
  };
  let roots = Array.from(document.querySelectorAll('[data-message-id],[data-msg-id]'));
  const currentLayout = roots.length === 0;
  if (currentLayout) {
    roots = Array.from(document.querySelectorAll('[data-index]')).filter(root =>
      root.querySelector('[data-e2e="msg-item-content"]') &&
      !root.querySelector('[data-e2e="conversation-item"]')
    );
  }
  const messages = [];
  for (const root of roots) {
    if (!rendered(root)) continue;
    const content = currentLayout
      ? root.querySelector('[data-e2e="msg-item-content"]')
      : root;
    if (!content) continue;

    const classNames = Array.from(root.querySelectorAll('[class]')).map(el =>
      String(el.className?.baseVal || el.className || '')
    ).join(' ');
    const rootText = String(root.innerText || root.textContent || '').trim();
    const greeting = /MessageItemHiGroup/.test(classNames);
    const filtered = /FilteredFilteredMessage/.test(classNames);
    const textNode = greeting ? root.querySelector('[class*="MessageItemHititle"]') : root.querySelector(
      '[class*="TextMessageTexttextInnerContent"],' +
      '[class*="MessageItemTextbubbleTextContent"],' +
      '[class*="im-text-inner"]'
    );
    let text = String(
      textNode?.innerText || textNode?.textContent ||
      content.innerText || content.textContent || ''
    ).trim();
    const own = currentLayout
      ? classNames.includes('isFromMe')
      : content.getBoundingClientRect().left > innerWidth / 2 - 10;

    const senderNameNode = root.querySelector(
      '[class*="MessageBoxMessageTitleavatarName"],[class*="avatarName"]'
    );
    let senderName = String(
      senderNameNode?.innerText || senderNameNode?.textContent || ''
    ).trim();
    const senderAvatarNode = root.querySelector(
      '[class*="commonIMAvatar"] img,span[class*="semi-avatar"] img,img[class*="avatar"]'
    );
    const senderAvatar = imageUrl(senderAvatarNode);

    const contentImages = Array.from(content.querySelectorAll('img'))
      .map(el => ({el, url: imageUrl(el)})).filter(row => row.url);
    let kind = 'text';
    if (greeting || filtered) {
      kind = 'system';
      senderName = '系统';
    } else if (/MessageItemEmoji|TextMessageTextemoji/.test(classNames) && !text) {
      kind = 'emoji';
    } else if (/MessageItemVideo|BulletBulletVideo/.test(classNames)) {
      kind = 'video';
    } else if (/MessageItemShareAweme|MessageItemCommentShare|BulletGeneralCard|lynx-card/.test(classNames)) {
      kind = 'app_post';
    } else if (/MessageItemImage|ImageMessage/.test(classNames) || (contentImages.length && !text)) {
      kind = 'image';
    }

    let title = '';
    let description = '';
    if (/MessageItemCommentShare/.test(classNames)) {
      const titleNode = content.querySelector('[class*="MessageItemCommentSharecommentTitle"]');
      const descriptionNode = content.querySelector(
        '[class*="MessageItemCommentSharecommentTextInner"]'
      );
      title = String(titleNode?.innerText || titleNode?.textContent || '').trim();
      description = String(
        descriptionNode?.innerText || descriptionNode?.textContent || ''
      ).trim();
      text = [title, description].filter(Boolean).join('\n') || text;
    } else if (kind === 'app_post' || kind === 'video') {
      const cardTitleNode = content.querySelector(
        '[class*="authorName"],[class*="im-text-inner"],[class*="refText"]'
      );
      title = String(
        cardTitleNode?.innerText || cardTitleNode?.textContent || text || ''
      ).trim();
      text = title || text;
    } else if (kind === 'system') {
      title = greeting ? text : '抖音提示';
      description = filtered ? text : '';
    }
    if (kind === 'text' && textNode?.querySelector('img[class*="emoji"]') && text) {
      text += ' [表情]';
    }

    let mediaNode = null;
    if (kind === 'emoji') {
      mediaNode = content.querySelector(
        'img[class*="MessageItemEmojiimage"],img[class*="TextMessageTextemoji"],img'
      );
    } else if (kind === 'app_post' || kind === 'video') {
      mediaNode = content.querySelector(
        'img[class*="awemeContainer"],img[class*="cover"],img:not([class*="authorAvatar"])'
      );
    } else if (kind === 'image') {
      mediaNode = content.querySelector('img');
    }
    const mediaUrl = kind === 'system'
      ? '' : (imageUrl(mediaNode) || contentImages[0]?.url || '');
    const linkNode = content.querySelector('a[href]');
    let url = reactCardUrl(content);
    const href = String(linkNode?.getAttribute('href') || '').trim();
    if (!url && href) {
      try {
        const parsed = new URL(href, location.origin);
        const host = parsed.hostname.toLowerCase();
        const allowed = host === 'douyin.com' || host.endsWith('.douyin.com');
        url = parsed.protocol === 'https:' && allowed && parsed.pathname !== '/'
          ? parsed.href : '';
      } catch (_error) {
        url = '';
      }
    }

    if (!text) {
      text = {
        emoji: '[表情]',
        image: '[图片]',
        video: '[视频]',
        app_post: '[分享内容]',
        system: '[系统消息]',
      }[kind] || '';
    }
    if (!text || text.length > 2000) continue;
    const timeNode = root.querySelector('[class*="MessageBoxTime"]');
    const timeText = String(timeNode?.innerText || timeNode?.textContent || '').trim();
    const stable = currentLayout
      ? (reactMessageId(root) || rootText || [own, senderName, timeText, kind, text, mediaUrl].join('\n'))
      : String(root.getAttribute('data-message-id') || root.getAttribute('data-msg-id') || '');
    if (!stable) continue;
    const rootRect = root.getBoundingClientRect();
    messages.push({
      text,
      kind,
      title,
      description,
      display_time: timeText,
      media_url: mediaUrl,
      url,
      video_url: kind === 'video' ? url : '',
      own,
      sender_name: senderName,
      sender_avatar: senderAvatar,
      top: rootRect.top,
      stable,
    });
  }
  messages.sort((a, b) => a.top - b.top);
  const titleCandidates = Array.from(document.querySelectorAll(
    '[class*="StackLayoutStackChatHeadertitle"],'
    + '[class*="StackLayoutStackTitleBarleftTitle"]'
  )).filter(el => {
    const r = el.getBoundingClientRect();
    return rendered(el) && topmost(el) && r.top >= 40 && r.top <= 180 &&
      r.left > innerWidth - 480 &&
      String(el.innerText || '').trim();
  });
  return {
    messages: messages.slice(-200),
    title: titleCandidates.length ? String(titleCandidates[0].innerText || '').trim() : '',
    url: location.href,
  };
}
"""

_VERIFY_CONVERSATION_SCRIPT = r"""
(expectedName) => {
  const expected = String(expectedName || '').trim();
  if (!expected) return false;
  const normalize = (value) => String(value || '').replace(/\(\d+\)/g, '').replace(/\s+/g, '');
  const topmost = (el) => {
    const r = el.getBoundingClientRect();
    const hit = document.elementFromPoint(
      Math.max(0, Math.min(innerWidth - 1, r.left + r.width / 2)),
      Math.max(0, Math.min(innerHeight - 1, r.top + r.height / 2))
    );
    return Boolean(hit && (el.contains(hit) || hit.contains(el)));
  };
  return Array.from(document.querySelectorAll(
    '[class*="StackLayoutStackTitleBarleftTitle"],'
    + '[class*="StackLayoutStackChatHeadertitle"]'
  ))
    .some(el => {
      const r = el.getBoundingClientRect();
      const s = getComputedStyle(el);
      const text = String(el.innerText || el.textContent || '').trim();
      return r.top >= 4 && r.top <= 180 && r.left > 220 && r.width > 10 &&
        s.display !== 'none' && s.visibility !== 'hidden' && topmost(el) &&
        (text === expected || normalize(text) === normalize(expected));
    });
}
"""



class DouyinBrowserImClient:
    """Serialize one verified personal-Douyin web inbox on a dedicated browser thread.

    The client deliberately does not copy the reference project's unofficial protocol code.
    It fails closed when account identity, conversation UID, stable message IDs, or a new
    outgoing bubble cannot be proven from the rendered page.
    """

    def __init__(self, account: DouyinAccount, *, clock: Callable[[], float] = time.time) -> None:
        self.account = account
        self._clock = clock
        self._commands: queue.Queue[_Command] = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._on_event: Callable[[DouyinInboundEvent], None] | None = None
        self._on_conversation: Callable[[DouyinConversation], None] | None = None
        self._on_history: Callable[[DouyinConversationHistory], None] | None = None
        self._row_previews: dict[str, str] = {}
        self._row_refresh_cursor = 0
        self._dom_baselined: set[str] = set()
        self._delivery_checks_lock = threading.Lock()
        self._delivery_checks_path = account.resolved_data_dir / "delivery_checks.json"
        self._delivery_checks = self._load_delivery_checks()
        self._status_lock = threading.Lock()
        self._status = {
            "credentials_valid": False,
            "identity_fingerprint": "",
            "identity_verified": False,
            "receive_connected": False,
            "last_error": "",
            "last_scan_at": 0,
            "normal_session_count": 0,
            "stranger_session_count": 0,
        }

    def start(
        self,
        on_event: Callable[[DouyinInboundEvent], None],
        on_conversation: Callable[[DouyinConversation], None] | None = None,
        on_history: Callable[[DouyinConversationHistory], None] | None = None,
    ) -> None:
        if self._thread is not None:
            return
        self._on_event = on_event
        self._on_conversation = on_conversation
        self._on_history = on_history
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name=f"douyin-browser-{self.account.account_id}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._commands.put(_Command("stop"))
        if self._thread is not None:
            self._thread.join(timeout=8.0)
            self._thread = None
        self._on_event = None
        self._on_conversation = None
        self._on_history = None

    def status(self) -> dict:
        with self._status_lock:
            return dict(self._status)

    def send_text(
        self,
        target: dict,
        text: str,
        *,
        allow_dom_target: bool = False,
        allow_dom_name_fallback: bool = False,
        timeout_s: float = 20.0,
    ) -> DouyinDeliveryReceipt:
        result = self._request(
            "send",
            target,
            text,
            bool(allow_dom_target),
            bool(allow_dom_name_fallback),
            timeout_s=timeout_s,
        )
        if not isinstance(result, DouyinDeliveryReceipt):
            raise DouyinBrowserError("抖音发送没有返回有效回执")
        return result

    def read_conversation(self, target: dict, *, timeout_s: float = 12.0) -> list[dict]:
        result = self._request("read", target, timeout_s=timeout_s)
        return list(result) if isinstance(result, list) else []

    def scan_now(self, *, timeout_s: float = 15.0) -> int:
        result = self._request("scan", timeout_s=timeout_s)
        return int(result or 0)

    def _request(self, operation: str, *args, timeout_s: float) -> object:
        if self._thread is None:
            raise DouyinBrowserError("抖音浏览器尚未启动")
        command = _Command(
            operation,
            args,
            deadline=time.monotonic() + timeout_s,
            client_message_id=uuid.uuid4().hex if operation == "send" else "",
        )
        self._commands.put(command)
        if not command.event.wait(timeout_s):
            with command.lock:
                if operation == "send" and command.side_effect_started:
                    return DouyinDeliveryReceipt(
                        command.client_message_id,
                        "unknown",
                        "发送操作已开始但等待回执超时；必须先从网页历史对账，不能直接重发",
                    )
                command.cancelled = True
            raise DouyinBrowserError(f"抖音浏览器操作超时且已取消：{operation}")
        if command.error is not None:
            raise DouyinBrowserError(str(command.error)) from command.error
        return command.result

    def _set_status(self, **changes) -> None:
        with self._status_lock:
            self._status.update(changes)
            snapshot = {
                key: value
                for key, value in self._status.items()
                if key != "identity_fingerprint"
            }
        try:
            path = self.account.resolved_data_dir / "browser_status.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix(".json.tmp")
            temp.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(path)
        except OSError:
            pass

    def _run(self) -> None:
        try:
            from playwright.sync_api import sync_playwright

            self.account.resolved_data_dir.mkdir(parents=True, exist_ok=True)
            self.account.resolved_profile_dir.mkdir(parents=True, exist_ok=True)
            with sync_playwright() as playwright:
                options = {
                    "user_data_dir": str(self.account.resolved_profile_dir.resolve()),
                    "headless": bool(self.account.headless),
                    "viewport": {"width": 1360, "height": 900},
                    "args": ["--disable-blink-features=AutomationControlled"],
                }
                if self.account.browser_channel:
                    options["channel"] = self.account.browser_channel
                context = playwright.chromium.launch_persistent_context(**options)
                page = context.pages[0] if context.pages else context.new_page()
                page.set_default_timeout(int(self.account.navigation_timeout_s * 1000))
                page.goto(self.account.messages_url, wait_until="domcontentloaded")
                next_scan = 0.0
                initial_scan = True
                self._set_status(receive_connected=True, last_error="")
                while not self._stop.is_set():
                    command = self._next_command(max(0.0, min(0.25, next_scan - time.monotonic())))
                    if command is not None:
                        if command.operation == "stop":
                            break
                        self._handle_command(page, context, command, initial_scan)
                        if command.operation == "scan" and command.error is None:
                            initial_scan = False
                            next_scan = time.monotonic() + self.account.poll_interval_s
                        continue
                    if time.monotonic() < next_scan:
                        continue
                    try:
                        if self._refresh_identity(context):
                            self._scan(page, initial_scan=initial_scan)
                            initial_scan = False
                    except Exception as exc:  # noqa: BLE001 - allow login/UI recovery
                        self._set_status(last_error=str(exc)[:500])
                    next_scan = time.monotonic() + self.account.poll_interval_s
                context.close()
        except Exception as exc:  # noqa: BLE001 - surfaced via health/status
            self._set_status(
                receive_connected=False,
                credentials_valid=False,
                identity_verified=False,
                last_error=str(exc)[:500],
            )
        finally:
            self._fail_pending(DouyinBrowserError("抖音浏览器已停止"))
            self._set_status(receive_connected=False, identity_verified=False)

    def _next_command(self, timeout: float) -> _Command | None:
        try:
            return self._commands.get(timeout=max(0.01, timeout))
        except queue.Empty:
            return None

    def _handle_command(self, page, context, command: _Command, initial_scan: bool) -> None:
        try:
            with command.lock:
                if command.cancelled or time.monotonic() >= command.deadline:
                    raise DouyinBrowserError("浏览器操作在执行前已取消")
            if not self._refresh_identity(context):
                raise DouyinBrowserError(self.status().get("last_error") or "抖音账号身份未验证")
            if command.operation == "send":
                allow_dom_target = bool(command.args[2]) if len(command.args) > 2 else False
                allow_dom_name_fallback = (
                    bool(command.args[3]) if len(command.args) > 3 else False
                )
                command.result = self._send(
                    page,
                    command.args[0],
                    str(command.args[1]),
                    command,
                    allow_dom_target=allow_dom_target,
                    allow_dom_name_fallback=allow_dom_name_fallback,
                )
            elif command.operation == "read":
                command.result = self._read(page, command.args[0])
            elif command.operation == "scan":
                command.result = self._scan(page, initial_scan=initial_scan)
            else:
                raise DouyinBrowserError(f"不支持的浏览器操作：{command.operation}")
        except BaseException as exc:  # noqa: BLE001 - propagate to requesting thread
            command.error = exc
        finally:
            command.event.set()

    def _refresh_identity(self, context) -> bool:
        cookies = context.cookies("https://www.douyin.com/")
        values = {str(row.get("name") or ""): str(row.get("value") or "") for row in cookies}
        logged_in = bool(values.get("sessionid"))
        material = next(
            (values.get(name, "") for name in ("uid_tt", "uid_tt_ss", "sid_tt", "sessionid")
             if values.get(name)),
            "",
        )
        fingerprint = hashlib.sha256(material.encode("utf-8")).hexdigest()[:16] if material else ""
        expected = self.account.expected_identity_fingerprint
        verified = bool(logged_in and fingerprint and expected and fingerprint == expected)
        if not logged_in:
            error = "请在打开的 Chrome 中登录个人抖音账号"
        elif not fingerprint:
            error = "无法取得登录身份指纹，收发均已锁定"
        elif not expected:
            error = f"账号身份尚未绑定；请把身份指纹 {fingerprint} 填入账号配置后重启"
        elif not verified:
            error = "当前 Chrome 登录身份与账号配置不一致，收发均已锁定"
        else:
            error = ""
        self._set_status(
            credentials_valid=logged_in,
            identity_fingerprint=fingerprint,
            identity_verified=verified,
            last_error=error,
        )
        return verified

    @staticmethod
    def _normalise_scan_rows(rows: object) -> list[dict]:
        normalised: list[dict] = []
        if not isinstance(rows, list):
            return normalised
        for index, value in enumerate(rows):
            if not isinstance(value, dict):
                continue
            row = dict(value)
            uid = str(row.get("uid") or "").strip()
            identity_seed = str(row.get("identity_seed") or "").strip()
            if not uid and identity_seed:
                digest = hashlib.sha256(identity_seed.encode("utf-8")).hexdigest()[:24]
                uid = f"dom:{digest}"
            name = str(row.get("name") or "").strip()
            if not uid or not name:
                continue
            row.update(
                uid=uid,
                name=name,
                index=max(0, int(row.get("index") or index)),
                locator_kind=str(row.get("locator_kind") or "stable_uid"),
            )
            row.pop("identity_seed", None)
            normalised.append(row)
        return normalised

    @staticmethod
    def _return_to_main_inbox(page) -> bool:
        for _attempt in range(3):
            if page.evaluate(_MAIN_INBOX_VISIBLE_SCRIPT) is True:
                return True
            if page.evaluate(_CLICK_VISIBLE_BACK_SCRIPT) is not True:
                return False
            button = page.locator('[data-acs-douyin-visible-back="true"]')
            if button.count() != 1:
                return False
            try:
                # Douyin animates the stacked panels while the SVG back button moves.
                # A normal Playwright click waits for geometric stability and can time
                # out before any send side effect begins. Event dispatch still bubbles
                # through React, while the next loop iteration verifies navigation.
                button.first.dispatch_event("click")
            except Exception:  # noqa: BLE001 - retry a detached transition node
                page.wait_for_timeout(150)
                continue
            page.wait_for_timeout(350)
        return False

    @staticmethod
    def _open_stranger_list(page) -> bool:
        locator = getattr(page, "locator", None)
        if callable(locator):
            entries = locator('[class*="conversationStrangerBoxwrapper"]')
            if entries.count() == 0:
                if page.evaluate(_SCROLL_INBOX_TO_TOP_SCRIPT) is True:
                    page.wait_for_timeout(200)
                entries = locator('[class*="conversationStrangerBoxwrapper"]')
            for index in range(min(entries.count(), 10)):
                try:
                    entries.nth(index).scroll_into_view_if_needed(timeout=1000)
                except Exception:  # noqa: BLE001 - skip stale/hidden duplicate React nodes
                    continue
        if page.evaluate(_OPEN_STRANGER_LIST_SCRIPT) is not True:
            return False
        entry = page.locator('[data-acs-douyin-stranger-entry="true"]')
        if entry.count() != 1:
            return False
        entry.first.click(timeout=3000)
        for _attempt in range(8):
            page.wait_for_timeout(250)
            if page.evaluate(_STRANGER_LIST_VISIBLE_SCRIPT) is True:
                return True
        return False

    def _scan(self, page, *, initial_scan: bool) -> int:
        self._return_to_main_inbox(page)
        rows = self._normalise_scan_rows(page.evaluate(_SCAN_LIST_SCRIPT) or [])
        opened_inbox = False
        if not rows:
            open_result = str(page.evaluate(_OPEN_INBOX_SCRIPT) or "")
            opened_inbox = bool(open_result)
            if open_result == "target":
                inbox_target = page.locator('[data-acs-douyin-inbox-target="true"]')
                if inbox_target.count() == 0:
                    opened_inbox = False
                else:
                    inbox_target.first.click()
            if opened_inbox:
                page.wait_for_timeout(900)
                self._return_to_main_inbox(page)
                rows = self._normalise_scan_rows(page.evaluate(_SCAN_LIST_SCRIPT) or [])
                # The current Douyin drawer animates in after the click. On slower
                # machines its conversation items appear around 1.5 seconds later,
                # so one fixed 900 ms read falsely reports an empty inbox.
                for _attempt in range(6):
                    if rows:
                        break
                    page.wait_for_timeout(250)
                    self._return_to_main_inbox(page)
                    rows = self._normalise_scan_rows(
                        page.evaluate(_SCAN_LIST_SCRIPT) or []
                    )
        if not rows:
            detail = (
                "已自动打开抖音‘消息’面板，但没有找到可识别的会话项"
                if opened_inbox
                else "找不到抖音‘消息’入口或私信会话列表"
            )
            raise DouyinBrowserError(
                f"{detail}；请确认网页消息面板已经显示后重试"
            )
        normal_rows = []
        for row in rows:
            row["list_scope"] = "inbox"
            normal_rows.append(row)
        stranger_rows: list[dict] = []
        if self._open_stranger_list(page):
            stranger_rows = self._normalise_scan_rows(
                page.evaluate(_SCAN_LIST_SCRIPT) or []
            )
            for row in stranger_rows:
                row["list_scope"] = "stranger"
            self._return_to_main_inbox(page)
        deduplicated: dict[str, dict] = {}
        for row in normal_rows:
            deduplicated.setdefault(str(row.get("uid") or ""), row)
        for row in stranger_rows:
            deduplicated[str(row.get("uid") or "")] = row
        rows = [row for uid, row in deduplicated.items() if uid]
        emitted = 0
        next_previews: dict[str, str] = {}
        prepared_rows: list[tuple[dict, dict, str, bool]] = []
        stable_rows = [
            row for row in rows
            if isinstance(row, dict) and str(row.get("uid") or "").strip()
        ]
        refresh_uid = ""
        if stable_rows and not initial_scan:
            refresh_row = stable_rows[self._row_refresh_cursor % len(stable_rows)]
            refresh_uid = str(refresh_row.get("uid") or "").strip()
            self._row_refresh_cursor = (self._row_refresh_cursor + 1) % len(stable_rows)

        # Register the complete visible inbox before opening any individual chat.
        # A changed/unsupported chat must never prevent the remaining sessions
        # from reaching the workbench.
        for row in rows:
            if not isinstance(row, dict):
                continue
            uid = str(row.get("uid") or "").strip()
            if not uid:
                continue
            preview = str(row.get("preview") or "")
            target = {
                "uid": uid,
                "name": str(row.get("name") or uid),
                "index": max(0, int(row.get("index") or 0)),
                "locator_kind": str(row.get("locator_kind") or "stable_uid"),
                "list_scope": str(row.get("list_scope") or "inbox"),
            }
            changed = self._row_previews.get(uid) != preview
            next_previews[uid] = preview
            prepared_rows.append((row, target, preview, changed))
            conversation_callback = self._on_conversation
            if conversation_callback is not None:
                conversation_callback(DouyinConversation(
                    conversation_id=uid,
                    name=target["name"],
                    preview=preview,
                    target=target,
                    avatar_url=str(row.get("avatar_url") or ""),
                ))

        row_errors: list[str] = []
        for row, target, _preview, changed in prepared_rows:
            uid = target["uid"]
            if not (initial_scan or row.get("unread") or changed or uid == refresh_uid):
                continue
            try:
                self._open_target(page, target)
                payload = page.evaluate(_READ_CHAT_SCRIPT) or {}
            except Exception as exc:  # noqa: BLE001 - isolate one changed chat from the inbox list
                row_errors.append(str(exc))
                continue
            messages = self._normalise_history_messages(target, payload)
            history_callback = self._on_history
            if history_callback is not None:
                history_callback(DouyinConversationHistory(uid, messages))
            event_initial_scan = initial_scan or (
                target["locator_kind"] == "conversation_item"
                and uid not in self._dom_baselined
            )
            for incoming in messages:
                if incoming.get("is_self"):
                    continue
                stable = str(incoming.get("message_id") or "").strip()
                text = str(incoming.get("text") or "").strip()
                if not stable or not text:
                    continue
                event = DouyinInboundEvent(
                    message_key=stable,
                    conversation_id=uid,
                    sender_id=uid,
                    sender_name=str(
                        incoming.get("sender_name") or target["name"]
                    ),
                    text=text,
                    timestamp=int(self._clock()),
                    target=target,
                    kind=str(incoming.get("kind") or "text"),
                    media_url=str(incoming.get("media_url") or ""),
                    title=str(incoming.get("title") or ""),
                    description=str(incoming.get("description") or ""),
                    url=str(incoming.get("url") or ""),
                    display_time=str(incoming.get("display_time") or ""),
                    sender_avatar=str(incoming.get("sender_avatar") or ""),
                    initial_scan=event_initial_scan,
                )
                callback = self._on_event
                if callback is not None:
                    callback(event)
                    emitted += 1
            if target["locator_kind"] == "conversation_item":
                self._dom_baselined.add(uid)
        self._row_previews = next_previews
        self._set_status(
            last_scan_at=int(self._clock()),
            last_error=row_errors[0][:500] if row_errors else "",
            normal_session_count=len(normal_rows),
            stranger_session_count=len(stranger_rows),
        )
        return emitted

    @classmethod
    def _candidate_from_current_rows(
        cls, page, rows: list[dict], uid: str, target_name: str
    ):
        matches = [row for row in rows if str(row.get("uid") or "") == uid]
        if len(matches) != 1:
            return None
        scanned_name = next(
            (
                line.strip()
                for line in str(matches[0].get("name") or "").splitlines()
                if line.strip()
            ),
            "",
        )
        if not target_name or scanned_name != target_name:
            return None
        candidates = page.locator('[data-acs-douyin-active-index]')
        index = max(0, int(matches[0].get("index") or 0))
        if index >= candidates.count():
            return None
        return candidates.nth(index)

    @classmethod
    def _find_dom_conversation(
        cls,
        page,
        uid: str,
        target_name: str,
        *,
        allow_name_fallback: bool = True,
    ):
        """Find a DOM-fingerprint conversation across Douyin's virtualized list."""
        seen_name_uids: set[str] = set()
        ambiguous_name = False

        def inspect_current_view():
            nonlocal ambiguous_name
            rows = cls._normalise_scan_rows(page.evaluate(_SCAN_LIST_SCRIPT) or [])
            name_rows = [
                row for row in rows
                if next(
                    (
                        line.strip()
                        for line in str(row.get("name") or "").splitlines()
                        if line.strip()
                    ),
                    "",
                ) == target_name
            ]
            seen_name_uids.update(str(row.get("uid") or "") for row in name_rows)
            if len(name_rows) > 1:
                ambiguous_name = True
            return rows, cls._candidate_from_current_rows(page, rows, uid, target_name)

        # First inspect the current viewport so common sends do not disturb list position.
        _rows, candidate = inspect_current_view()
        if candidate is not None:
            return candidate

        page.evaluate(_SCROLL_ACTIVE_CONVERSATION_LIST_SCRIPT, "top")
        page.wait_for_timeout(250)
        seen_views: set[tuple[str, ...]] = set()
        for _attempt in range(30):
            rows, candidate = inspect_current_view()
            if candidate is not None:
                return candidate
            signature = tuple(str(row.get("uid") or "") for row in rows)
            if signature and signature in seen_views:
                break
            if signature:
                seen_views.add(signature)
            movement = page.evaluate(_SCROLL_ACTIVE_CONVERSATION_LIST_SCRIPT, "next") or {}
            if not bool(movement.get("moved")):
                break
            page.wait_for_timeout(200)

        # A changed avatar can rotate the DOM fingerprint. Automated sending must
        # stop here; only an explicit human action may opt into the unique-name
        # recovery path below.
        if not allow_name_fallback:
            return None

        # For an explicit human action, fall back only after a complete traversal
        # proves the cleaned display name globally unique.
        seen_name_uids.discard("")
        if ambiguous_name or len(seen_name_uids) != 1:
            return None
        fallback_uid = next(iter(seen_name_uids))
        page.evaluate(_SCROLL_ACTIVE_CONVERSATION_LIST_SCRIPT, "top")
        page.wait_for_timeout(250)
        seen_views.clear()
        for _attempt in range(30):
            rows = cls._normalise_scan_rows(page.evaluate(_SCAN_LIST_SCRIPT) or [])
            candidate = cls._candidate_from_current_rows(
                page, rows, fallback_uid, target_name
            )
            if candidate is not None:
                return candidate
            signature = tuple(str(row.get("uid") or "") for row in rows)
            if signature and signature in seen_views:
                break
            if signature:
                seen_views.add(signature)
            movement = page.evaluate(_SCROLL_ACTIVE_CONVERSATION_LIST_SCRIPT, "next") or {}
            if not bool(movement.get("moved")):
                break
            page.wait_for_timeout(200)
        return None

    @classmethod
    def _open_target(
        cls,
        page,
        target: dict,
        *,
        allow_dom_name_fallback: bool = True,
    ) -> None:
        uid = str(target.get("uid") or "").strip()
        if not uid:
            raise DouyinBrowserError("抖音会话缺少稳定用户 ID，拒绝定位")
        locator_kind = str(target.get("locator_kind") or "stable_uid")
        target_name = str(target.get("name") or "").strip()
        if locator_kind == "conversation_item":
            target_name = next(
                (line.strip() for line in target_name.splitlines() if line.strip()),
                target_name,
            )
        locator = None
        if locator_kind == "conversation_item":
            cls._return_to_main_inbox(page)
            list_scope = str(target.get("list_scope") or "inbox")
            if list_scope == "stranger":
                if not cls._open_stranger_list(page):
                    raise DouyinBrowserError("找不到抖音陌生人消息入口")
            page.wait_for_timeout(200)
            locator = cls._find_dom_conversation(
                page,
                uid,
                target_name,
                allow_name_fallback=allow_dom_name_fallback,
            )
        else:
            candidates = page.locator('[role="listitem"], [data-uid], [data-user-id]')
            for index in range(min(candidates.count(), 200)):
                candidate = candidates.nth(index)
                if uid in {
                    str(candidate.get_attribute("data-uid") or ""),
                    str(candidate.get_attribute("data-user-id") or ""),
                }:
                    locator = candidate
                    break
        if locator is None:
            raise DouyinBrowserError("找不到唯一匹配的抖音会话，拒绝按昵称或列表索引盲目定位")
        locator.scroll_into_view_if_needed()
        locator.click()
        page.wait_for_timeout(450)
        expected_name = target_name
        if not expected_name or not page.evaluate(_VERIFY_CONVERSATION_SCRIPT, expected_name):
            raise DouyinBrowserError("会话标题与稳定用户定位不一致，拒绝读取或发送")

    def _read(
        self,
        page,
        target: dict,
        *,
        allow_dom_name_fallback: bool = True,
    ) -> list[dict]:
        self._open_target(
            page,
            target,
            allow_dom_name_fallback=allow_dom_name_fallback,
        )
        payload = page.evaluate(_READ_CHAT_SCRIPT) or {}
        return self._normalise_history_messages(target, payload)

    def _read_open_conversation(self, page, target: dict) -> list[dict]:
        """Read the already verified chat without navigating the virtualized list again."""
        payload = page.evaluate(_READ_CHAT_SCRIPT) or {}
        return self._normalise_history_messages(target, payload)

    def _normalise_history_messages(self, target: dict, payload: object) -> list[dict]:
        uid = str(target.get("uid") or "")
        locator_kind = str(target.get("locator_kind") or "stable_uid")
        rows: list[dict] = []
        messages = payload.get("messages", []) if isinstance(payload, dict) else []
        for sequence, source in enumerate(messages if isinstance(messages, list) else []):
            if not isinstance(source, dict):
                continue
            stable = str(source.get("stable") or "").strip()
            text = str(source.get("text") or "").strip()
            if not stable or not text:
                continue
            message_id = (
                stable
                if locator_kind == "stable_uid"
                else hashlib.sha256(f"{uid}\n{stable}".encode("utf-8")).hexdigest()
            )
            rows.append({
                "text": text,
                "kind": str(source.get("kind") or "text"),
                "title": str(source.get("title") or ""),
                "description": str(source.get("description") or ""),
                "media_url": str(source.get("media_url") or ""),
                "url": str(source.get("url") or ""),
                "video_url": str(source.get("video_url") or ""),
                "display_time": str(source.get("display_time") or ""),
                "sender_name": str(
                    source.get("sender_name")
                    or (payload.get("title") if isinstance(payload, dict) else "")
                    or target.get("name")
                    or ""
                ),
                "sender_avatar": str(source.get("sender_avatar") or ""),
                "is_self": bool(source.get("own")),
                "ts": 0,
                "message_id": message_id,
                "sequence": sequence,
            })
        with self._delivery_checks_lock:
            checks = list(self._delivery_checks.values())
        for row in rows:
            if not row["is_self"]:
                continue
            matching = [
                int(check.get("started_at") or 0)
                for check in checks
                if str(check.get("uid") or "") == uid
                and str(check.get("text") or "") == row["text"]
                and row["message_id"] not in set(check.get("before_ids") or [])
            ]
            if matching:
                row["ts"] = max(matching)
        return rows

    def _send(
        self,
        page,
        target: dict,
        text: str,
        command: _Command,
        *,
        allow_dom_target: bool = False,
        allow_dom_name_fallback: bool = False,
    ) -> DouyinDeliveryReceipt:
        client_message_id = command.client_message_id
        if not text.strip():
            return DouyinDeliveryReceipt(client_message_id, "failed", "回复内容为空")
        if len(text) > 1000:
            return DouyinDeliveryReceipt(client_message_id, "failed", "回复超过 1000 字安全上限")
        if not str(target.get("uid") or "").strip():
            return DouyinDeliveryReceipt(client_message_id, "failed", "会话缺少稳定用户 ID")
        locator_kind = str(target.get("locator_kind") or "stable_uid")
        if locator_kind != "stable_uid" and not (
            allow_dom_target and locator_kind == "conversation_item"
        ):
            return DouyinDeliveryReceipt(
                client_message_id,
                "failed",
                "当前抖音网页只提供会话 DOM 指纹；本次发送未获得精确 DOM 会话授权，已拒绝",
            )
        before = self._read(
            page,
            target,
            allow_dom_name_fallback=allow_dom_name_fallback,
        )
        before_ids = {str(row.get("message_id") or "") for row in before}
        editors = page.locator('div[contenteditable="true"][role="textbox"]')
        if editors.count() == 0:
            editors = page.locator('div[contenteditable="true"]')
        if editors.count() == 0:
            return DouyinDeliveryReceipt(client_message_id, "failed", "找不到抖音私信输入框")
        with command.lock:
            if command.cancelled or time.monotonic() >= command.deadline:
                return DouyinDeliveryReceipt(client_message_id, "failed", "发送在产生副作用前已取消")
            command.side_effect_started = True
        self._register_delivery_check(
            command.client_message_id,
            uid=str(target.get("uid") or ""),
            text=text,
            before_ids=before_ids,
        )
        editor = editors.last
        editor.click()
        editor.fill(text)
        editor.press("Enter")
        confirm_deadline = min(command.deadline, time.monotonic() + 5.0)
        while time.monotonic() < confirm_deadline:
            page.wait_for_timeout(350)
            # The target was verified immediately before typing. Reopening it on
            # every confirmation poll traverses Douyin's virtualized list and can
            # consume the entire receipt deadline even though the new bubble is
            # already visible in the current chat.
            messages = self._read_open_conversation(page, target)
            if any(
                row.get("is_self")
                and str(row.get("text") or "") == text
                and str(row.get("message_id") or "") not in before_ids
                for row in messages[-20:]
            ):
                return DouyinDeliveryReceipt(client_message_id, "confirmed")
        return DouyinDeliveryReceipt(
            client_message_id,
            "unknown",
            "已执行发送，但没有发现带新消息 ID 的本人气泡；不会标记为成功或直接重发",
        )

    def _load_delivery_checks(self) -> dict[str, dict]:
        try:
            payload = json.loads(self._delivery_checks_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        if not isinstance(payload, dict):
            return {}
        rows = payload.get("checks", {})
        if not isinstance(rows, dict):
            return {}
        return {
            str(key): dict(value)
            for key, value in list(rows.items())[-200:]
            if isinstance(value, dict)
        }

    def _register_delivery_check(
        self,
        client_message_id: str,
        *,
        uid: str,
        text: str,
        before_ids: set[str],
    ) -> None:
        with self._delivery_checks_lock:
            self._delivery_checks[client_message_id] = {
                "uid": uid,
                "text": text,
                "before_ids": sorted(value for value in before_ids if value),
                "started_at": int(self._clock()),
            }
            if len(self._delivery_checks) > 200:
                oldest = next(iter(self._delivery_checks))
                self._delivery_checks.pop(oldest, None)
            payload = {"version": 1, "checks": self._delivery_checks}
            path = self._delivery_checks_path
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix(f"{path.suffix}.tmp")
            temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(path)

    def _fail_pending(self, error: BaseException) -> None:
        while True:
            try:
                command = self._commands.get_nowait()
            except queue.Empty:
                return
            command.error = error
            command.event.set()
