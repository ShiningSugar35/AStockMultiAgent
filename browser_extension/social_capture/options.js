const bridgeUrl = document.getElementById("bridgeUrl");
const bridgeKey = document.getElementById("bridgeKey");
const statusNode = document.getElementById("status");

async function load() {
  const values = await chrome.storage.local.get({
    bridgeUrl: "http://127.0.0.1:8766",
    bridgeKey: "",
    enabled: true
  });
  bridgeUrl.value = values.bridgeUrl;
  bridgeKey.value = values.bridgeKey;
}

document.getElementById("save").addEventListener("click", async () => {
  const base = bridgeUrl.value.trim().replace(/\/+$/, "");
  const keyValue = bridgeKey.value.trim();
  if (!/^http:\/\/127\.0\.0\.1:\d{2,5}$/.test(base) || keyValue.length < 48) {
    statusNode.textContent = "请检查本地地址和连接密钥。";
    return;
  }
  await chrome.storage.local.set({ bridgeUrl: base, bridgeKey: keyValue, enabled: true });
  chrome.runtime.sendMessage({ type: "WAKE" }).catch(() => {});
  statusNode.textContent = "连接配置已保存。执行投研搜索时，本地桥接服务会自动分配搜索任务。";
});

load().catch((error) => {
  statusNode.textContent = "读取配置失败：" + String(error);
});
