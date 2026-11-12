const SELECTOR_VERSION = "dom-v1";

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function cleanText(value, limit = 12000) {
  return String(value || "").replace(/\s+/g, " ").trim().slice(0, limit);
}

function absoluteUrl(value) {
  try {
    const url = new URL(value, location.href);
    if (url.protocol !== "https:") return "";
    url.hash = "";
    return url.href;
  } catch (_error) {
    return "";
  }
}

function cardMatchesQuery(item, query, queryKind) {
  const terms = cleanText(query, 240).split(/\s+/).filter((term) => term.length >= 2);
  if (!terms.length) return false;
  const body = cleanText(item.title + " " + item.text, 16000).toLocaleLowerCase();
  // Company or manager queries must bind to the actual issuer. Search pages
  // sometimes show unrelated personalized recommendations in generic cards.
  if (body.includes(terms[0].toLocaleLowerCase())) return true;
  return queryKind === "INDUSTRY"
    && terms.slice(1).some((term) => body.includes(term.toLocaleLowerCase()));
}

function uniqueNodes(nodes) {
  const seen = new Set();
  return nodes.filter((node) => {
    if (!node || seen.has(node)) return false;
    seen.add(node);
    return true;
  });
}

function xueqiuCards() {
  const selectors = [
    ".timeline__item",
    "article[class*='timeline']",
    "div[class*='search-result']",
    "div[class*='SearchResult']"
  ];
  return uniqueNodes(selectors.flatMap((selector) => Array.from(document.querySelectorAll(selector))));
}

function zhihuCards() {
  const selectors = [
    ".SearchResult-Card",
    ".List-item",
    ".ContentItem",
    "div[data-za-detail-view-path]"
  ];
  return uniqueNodes(selectors.flatMap((selector) => Array.from(document.querySelectorAll(selector))));
}

// Zhihu search cards show a yearless "MM-DD" for some items. Interpret only
// the calendar day; store the original string for traceability and use the
// beginning of that Shanghai day, never an invented intraday publication time.
function inferZhihuVisibleDay(raw, capturedAt = new Date()) {
  const match = cleanText(raw).match(/^(\d{1,2})[-/](\d{1,2})$/);
  if (!match) return "";
  const month = Number(match[1]);
  const day = Number(match[2]);
  const displayYear = Number(new Intl.DateTimeFormat("en-US", {
    timeZone: "Asia/Shanghai", year: "numeric"
  }).format(capturedAt));
  const build = (year) =>
    new Date(`${year}-${String(month).padStart(2, "0")}-${String(day).padStart(2, "0")}T00:00:00+08:00`);
  let candidate = build(displayYear);
  if (!Number.isFinite(candidate.getTime())) return "";
  if (candidate > capturedAt) candidate = build(displayYear - 1);
  // Reject impossible dates (such as Feb 30) instead of silently rolling over.
  const localDay = new Date(candidate.getTime() + 8 * 3600000);
  if (localDay.getUTCMonth() + 1 !== month || localDay.getUTCDate() !== day) return "";
  return candidate.toISOString();
}

function extractTime(card, platform, capturedAt = new Date()) {
  const selectors = platform === "XUEQIU"
    ? ".date-and-source, time[datetime], time, [data-time], [data-timestamp], .timestamp"
    : ".SearchItem-time, .ContentItem-time, time[datetime], time, [data-time], [data-timestamp], .timestamp";
  const node = card.querySelector(selectors);
  if (!node) return { publishedAt: "", publishedRaw: "" };
  const publishedRaw = cleanText(
    node.getAttribute("title") || node.firstChild?.textContent
    || node.innerText || node.textContent || "", 160
  );
  const publishedAt = node.getAttribute("datetime")
    || node.getAttribute("data-time")
    || node.getAttribute("data-timestamp")
    || (platform === "ZHIHU" ? inferZhihuVisibleDay(publishedRaw, capturedAt) : "");
  return { publishedAt: cleanText(publishedAt, 100), publishedRaw };
}

function extractXueqiu(card, query) {
  // The result card's time link is the canonical post. The first stock link
  // often points to /S/SZ002475 and must not be treated as an article URL.
  const link = card.querySelector("a.date-and-source[href]")
    || card.querySelector("a[href*='/status/'], a[href*='/statuses/']");
  const url = link ? absoluteUrl(link.href) : "";
  if (!url || !/^https:\/\/(?:www\.)?xueqiu\.com\//.test(url)) return null;
  // A reply editor always contains "forward" in its class name; only the
  // actual forwarded-content container disqualifies an original post.
  const forwarded = Boolean(card.querySelector(
    ".timeline__item__forward, [data-is-repost='true']"
  ));
  const sourceText = card.querySelector(".date-and-source .source")?.textContent || "";
  const externalSource = /来自(?:新闻|微博|微信公众号|网站)/.test(sourceText);
  const text = cleanText(card.innerText || card.textContent);
  const titleNode = card.querySelector("h1, h2, h3, .title, .timeline__item__title");
  const authorNode = card.querySelector(".user-name, .username, [class*='user-name'], [class*='author']");
  const time = extractTime(card, "XUEQIU");
  return {
    url,
    title: cleanText(titleNode?.innerText || link.innerText || text.slice(0, 180), 1200),
    text,
    author: cleanText(authorNode?.innerText || "", 300),
    published_raw: time.publishedRaw,
    published_at: time.publishedAt,
    is_original: !forwarded && !externalSource,
    is_video: false,
    content_type: "STATUS",
    matched_query: query,
    raw_excerpt: cleanText(card.innerHTML, 12000)
  };
}

function extractZhihu(card, query) {
  const link = card.querySelector(
    "a[href*='/question/'], a[href*='/p/'], a[href*='/answer/'], a[href*='/zvideo/']"
  );
  const url = link ? absoluteUrl(link.href) : "";
  if (!url || !/^https:\/\/(?:www\.)?zhihu\.com\//.test(url)
      && !/^https:\/\/zhuanlan\.zhihu\.com\//.test(url)) return null;
  const video = Boolean(
    url.includes("/zvideo/")
    || card.querySelector("video, [class*='VideoCard'], [class*='video-card'], [data-za-detail-view-path*='zvideo']")
  );
  const text = cleanText(card.innerText || card.textContent);
  const titleNode = card.querySelector(
    ".ContentItem-title, .SearchResult-Card-title, h2, h3, [class*='title']"
  );
  const authorNode = card.querySelector(
    ".AuthorInfo-name, .UserLink-link, [class*='AuthorInfo'], [class*='author']"
  );
  const time = extractTime(card, "ZHIHU");
  let contentType = "CONTENT";
  if (url.includes("/answer/")) contentType = "ANSWER";
  else if (url.includes("/p/")) contentType = "ARTICLE";
  else if (url.includes("/question/")) contentType = "QUESTION";
  else if (url.includes("/zvideo/")) contentType = "VIDEO";
  return {
    url,
    title: cleanText(titleNode?.innerText || link.innerText || text.slice(0, 180), 1200),
    text,
    author: cleanText(authorNode?.innerText || "", 300),
    published_raw: time.publishedRaw,
    published_at: time.publishedAt,
    is_original: null,
    is_video: video,
    content_type: contentType,
    matched_query: query,
    raw_excerpt: cleanText(card.innerHTML, 12000)
  };
}

function noResultsDetected() {
  const text = document.body?.innerText || "";
  return /没有找到相关内容|暂无搜索结果|未找到相关内容|没有搜索到相关内容/.test(text);
}

function loginRequired() {
  const path = location.pathname.toLowerCase();
  if (path.includes("signin") || path.includes("login")) return true;
  const text = document.body?.innerText || "";
  return /登录后查看|请先登录|扫码登录|登录以继续/.test(text);
}

async function waitForResults(platform, timeoutMs = 10000) {
  const started = Date.now();
  let previous = -1;
  let stable = 0;
  while (Date.now() - started < timeoutMs) {
    const count = platform === "XUEQIU" ? xueqiuCards().length : zhihuCards().length;
    if (count > 0 && count === previous) stable += 1;
    else stable = 0;
    if (stable >= 2) return;
    previous = count;
    await sleep(500);
  }
}

async function extractPage(message) {
  await waitForResults(message.platform);
  if (loginRequired()) {
    return {
      query_id: message.queryId,
      query: message.query,
      query_kind: message.queryKind,
      page_url: location.href,
      page_title: document.title,
      captured_at: new Date().toISOString(),
      selector_version: SELECTOR_VERSION,
      parse_status: "LOGIN_REQUIRED",
      no_results_detected: false,
      items: [],
      has_more: false,
      scroll_rounds: 0,
      error_code: "LOGIN_REQUIRED"
    };
  }
  const extract = message.platform === "XUEQIU" ? extractXueqiu : extractZhihu;
  const cards = message.platform === "XUEQIU" ? xueqiuCards : zhihuCards;
  const records = new Map();
  let scrollRounds = 0;
  let stagnant = 0;
  let previousCount = cards().length;
  const maxScrolls = Math.max(0, Math.min(10, Number(message.maxScrolls || 0)));
  const maxItems = Math.max(1, Math.min(200, Number(message.maxItems || 80)));
  for (let round = 0; round <= maxScrolls; round += 1) {
    for (const card of cards()) {
      const item = extract(card, message.query);
      if (item && item.url
          && cardMatchesQuery(item, message.query, message.queryKind)
          && !records.has(item.url)) {
        records.set(item.url, item);
      }
      if (records.size >= maxItems) break;
    }
    if (records.size >= maxItems || round === maxScrolls) break;
    window.scrollTo(0, document.body.scrollHeight);
    await sleep(900);
    scrollRounds += 1;
    const count = cards().length;
    if (count <= previousCount) stagnant += 1;
    else stagnant = 0;
    previousCount = count;
    if (stagnant >= 2) break;
  }
  const items = Array.from(records.values()).slice(0, maxItems);
  const empty = noResultsDetected();
  const parsed = items.length > 0 || empty;
  const hasMore = items.length >= maxItems
    || (maxScrolls > 0 && scrollRounds >= maxScrolls && !empty)
    || Boolean(document.querySelector("button[class*='load'], a[class*='load-more'], [class*='loadMore']"));
  return {
    query_id: message.queryId,
    query: message.query,
    query_kind: message.queryKind,
    page_url: location.href,
    page_title: document.title,
    captured_at: new Date().toISOString(),
    selector_version: SELECTOR_VERSION,
    parse_status: parsed ? (items.length ? "PARSED" : "EMPTY") : "UNPARSED",
    no_results_detected: empty,
    items,
    has_more: hasMore,
    scroll_rounds: scrollRounds,
    error_code: parsed ? "" : "SEARCH_RESULT_DOM_NOT_RECOGNIZED"
  };
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (!message || message.type !== "EXTRACT_SOCIAL_PAGE") return false;
  extractPage(message).then(sendResponse).catch((error) => {
    sendResponse({
      query_id: message.queryId,
      query: message.query,
      query_kind: message.queryKind,
      page_url: location.href,
      page_title: document.title,
      captured_at: new Date().toISOString(),
      selector_version: SELECTOR_VERSION,
      parse_status: "ERROR",
      no_results_detected: false,
      items: [],
      has_more: false,
      scroll_rounds: 0,
      error_code: String(error).slice(0, 120)
    });
  });
  return true;
});
