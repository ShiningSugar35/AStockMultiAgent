"use strict";
// Isolated selection-regression tests for the Chrome extension's pure matcher.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(
  path.resolve(__dirname, "../../browser_extension/social_capture/content.js"),
  "utf8"
);
const sandbox = {
  chrome: { runtime: { onMessage: { addListener() {} } } },
  setTimeout,
  Promise,
  URL,
  location: { href: "https://www.zhihu.com/search?q=test" }
};
vm.runInNewContext(source, sandbox);
const matches = sandbox.cardMatchesQuery;
assert.equal(typeof matches, "function");
assert.equal(matches(
  { title: "立讯精密供应链分析", text: "立讯精密订单增长与价格讨论" },
  "立讯精密", "COMPANY"
), true);
assert.equal(matches(
  { title: "无关话题", text: "近期讨论天气与游戏" },
  "立讯精密", "COMPANY"
), false);
assert.equal(matches(
  { title: "某公司董事长", text: "董事长经营治理泛谈" },
  "立讯精密 董事长", "MANAGEMENT"
), false);
assert.equal(matches(
  { title: "AI铜连接产业发展", text: "行业上下游订单" },
  "立讯精密 AI铜连接", "INDUSTRY"
), true);
assert.equal(matches(
  { title: "其他公司发展", text: "近期财经热点" },
  "立讯精密 AI铜连接", "INDUSTRY"
), false);
// Real search cards use SearchItem-time (Zhihu) and date-and-source
// (Xueqiu); timestamp parsing must not guess an intraday publication time.
const captured = new Date("2026-11-11T06:00:00Z");
assert.equal(
  sandbox.inferZhihuVisibleDay("11-07", captured),
  "2026-11-06T16:00:00.000Z"
);
assert.equal(sandbox.inferZhihuVisibleDay("02-30", captured), "");
assert.equal(sandbox.inferZhihuVisibleDay("刚刚", captured), "");
const zhihuTime = sandbox.extractTime({
  querySelector(selector) {
    assert.ok(selector.includes(".SearchItem-time"));
    return {
      getAttribute() { return null; },
      firstChild: { textContent: "11-07" }
    };
  }
}, "ZHIHU", captured);
assert.equal(zhihuTime.publishedRaw, "11-07");
assert.equal(zhihuTime.publishedAt, "2026-11-06T16:00:00.000Z");

const postLink = {
  href: "https://xueqiu.com/9789610079/416268872",
  innerText: "昨天 14:24"
};
const statusDate = {
  getAttribute() { return null; },
  firstChild: { textContent: "昨天 14:24" }
};
const xueqiuCard = {
  innerText: "立讯精密公司近期发展讨论与行业供给变化",
  textContent: "立讯精密公司近期发展讨论与行业供给变化",
  innerHTML: "<div class='timeline__item__forward__editor'></div>",
  querySelector(selector) {
    if (selector === "a.date-and-source[href]") return postLink;
    if (selector.includes(".timeline__item__forward,")) return null;
    if (selector === ".date-and-source .source") {
      return { textContent: "· 来自雪球" };
    }
    if (selector.startsWith(".date-and-source,")) return statusDate;
    return null;
  }
};
const original = sandbox.extractXueqiu(xueqiuCard, "立讯精密");
assert.equal(original.url, postLink.href);
assert.equal(original.published_raw, "昨天 14:24");
assert.equal(original.is_original, true);
const forwardedCard = {
  ...xueqiuCard,
  querySelector(selector) {
    if (selector.includes(".timeline__item__forward,")) return {};
    return xueqiuCard.querySelector(selector);
  }
};
assert.equal(
  sandbox.extractXueqiu(forwardedCard, "立讯精密").is_original,
  false
);
process.stdout.write("social capture query, timestamp, post URL and original checks: PASS\n");
