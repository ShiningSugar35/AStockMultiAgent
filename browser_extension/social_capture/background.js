const ALARM_NAME = "astock-social-capture-poll";
let pumping = false;

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function readConfig() {
  return chrome.storage.local.get({
    bridgeUrl: "http://127.0.0.1:8766",
    bridgeKey: "",
    enabled: true
  });
}

async function bridgeFetch(path, config, options = {}) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 25000);
  try {
    return await fetch(config.bridgeUrl + path, {
      ...options,
      signal: controller.signal,
      cache: "no-store",
      headers: {
        "X-AStock-Bridge-Key": config.bridgeKey,
        ...(options.headers || {})
      }
    });
  } finally {
    clearTimeout(timeout);
  }
}

function waitForTabComplete(tabId, timeoutMs = 25000) {
  return new Promise((resolve, reject) => {
    let settled = false;
    const finish = (error) => {
      if (settled) return;
      settled = true;
      chrome.tabs.onUpdated.removeListener(listener);
      clearTimeout(timer);
      if (error) reject(error);
      else resolve();
    };
    const listener = (updatedId, changeInfo) => {
      if (updatedId === tabId && changeInfo.status === "complete") finish();
    };
    const timer = setTimeout(() => finish(new Error("PAGE_LOAD_TIMEOUT")), timeoutMs);
    chrome.tabs.onUpdated.addListener(listener);
    chrome.tabs.get(tabId).then((tab) => {
      if (tab.status === "complete") finish();
    }).catch(finish);
  });
}

async function captureTabQuery(tabId, query, job) {
  await chrome.tabs.update(tabId, { url: query.search_url, active: false });
  await waitForTabComplete(tabId);
  let lastError;
  for (let attempt = 0; attempt < 5; attempt += 1) {
    try {
      return await chrome.tabs.sendMessage(tabId, {
        type: "EXTRACT_SOCIAL_PAGE",
        platform: job.platform,
        query: query.text,
        queryId: query.query_id,
        queryKind: query.kind,
        maxScrolls: job.max_scrolls,
        maxItems: job.max_items_per_query
      });
    } catch (error) {
      lastError = error;
      await sleep(400);
    }
  }
  return {
    query_id: query.query_id,
    query: query.text,
    query_kind: query.kind,
    page_url: query.search_url,
    page_title: "",
    captured_at: new Date().toISOString(),
    selector_version: "dom-v1",
    parse_status: "ERROR",
    no_results_detected: false,
    items: [],
    has_more: false,
    scroll_rounds: 0,
    error_code: String(lastError || "CONTENT_SCRIPT_UNAVAILABLE").slice(0, 120)
  };
}

async function submitResult(job, queryResults, errorCode = "") {
  const config = await readConfig();
  if (!config.bridgeKey) return;
  const payload = {
    job_id: job.job_id,
    session_id: job.session_id,
    platform: job.platform,
    captured_at: new Date().toISOString(),
    extension_version: chrome.runtime.getManifest().version,
    query_results: queryResults,
    error_code: errorCode
  };
  for (let attempt = 0; attempt < 2; attempt += 1) {
    try {
      const response = await bridgeFetch("/v1/result", config, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      if (response.ok) return;
    } catch (_error) {
      await sleep(500);
    }
  }
}

async function executeJob(job) {
  let tabId = null;
  const queryResults = [];
  try {
    if (!Array.isArray(job.queries) || !job.queries.length) {
      throw new Error("EMPTY_SOCIAL_QUERY_BATCH");
    }
    const firstUrl = new URL(job.queries[0].search_url);
    const allowedHosts = job.platform === "XUEQIU"
      ? new Set(["xueqiu.com", "www.xueqiu.com"])
      : new Set(["www.zhihu.com", "zhuanlan.zhihu.com"]);
    if (!allowedHosts.has(firstUrl.hostname)) throw new Error("PLATFORM_HOST_MISMATCH");
    const tab = await chrome.tabs.create({ url: firstUrl.href, active: false });
    tabId = tab.id;
    await waitForTabComplete(tabId);
    for (let index = 0; index < job.queries.length; index += 1) {
      const query = job.queries[index];
      const parsed = new URL(query.search_url);
      if (!allowedHosts.has(parsed.hostname)) {
        queryResults.push({
          query_id: query.query_id,
          query: query.text,
          query_kind: query.kind,
          page_url: query.search_url,
          page_title: "",
          captured_at: new Date().toISOString(),
          selector_version: "dom-v1",
          parse_status: "ERROR",
          no_results_detected: false,
          items: [],
          has_more: false,
          scroll_rounds: 0,
          error_code: "PLATFORM_HOST_MISMATCH"
        });
        continue;
      }
      if (index > 0) {
        queryResults.push(await captureTabQuery(tabId, query, job));
      } else {
        try {
          queryResults.push(await chrome.tabs.sendMessage(tabId, {
            type: "EXTRACT_SOCIAL_PAGE",
            platform: job.platform,
            query: query.text,
            queryId: query.query_id,
            queryKind: query.kind,
            maxScrolls: job.max_scrolls,
            maxItems: job.max_items_per_query
          }));
        } catch (_error) {
          queryResults.push(await captureTabQuery(tabId, query, job));
        }
      }
    }
    await submitResult(job, queryResults);
  } catch (error) {
    await submitResult(job, queryResults, String(error).slice(0, 120));
  } finally {
    if (tabId !== null) {
      try { await chrome.tabs.remove(tabId); } catch (_error) {}
    }
  }
}

async function pump() {
  if (pumping) return;
  pumping = true;
  try {
    const config = await readConfig();
    if (!config.enabled || !config.bridgeKey) return;
    await bridgeFetch("/v1/heartbeat", config, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ version: chrome.runtime.getManifest().version })
    });
    // Each platform owns its own tab and independent capture result.
    // Run at most two platform jobs concurrently; never serialize both
    // four-query batches behind the same wall-clock deadline.
    const jobs = [];
    for (let slot = 0; slot < 2; slot += 1) {
      try {
        const response = await bridgeFetch("/v1/next?wait=1", config);
        if (response.status === 204 || !response.ok) break;
        const job = await response.json();
        jobs.push(executeJob(job));
      } catch (_error) {
        break;
      }
    }
    await Promise.allSettled(jobs);
  } finally {
    pumping = false;
  }
}

chrome.runtime.onInstalled.addListener(() => {
  chrome.alarms.create(ALARM_NAME, { periodInMinutes: 0.5 });
  chrome.runtime.openOptionsPage().catch(() => {});
  pump();
});
chrome.runtime.onStartup.addListener(() => {
  chrome.alarms.create(ALARM_NAME, { periodInMinutes: 0.5 });
  pump();
});
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === ALARM_NAME) pump();
});
chrome.runtime.onMessage.addListener((message) => {
  if (message && message.type === "WAKE") pump();
});
