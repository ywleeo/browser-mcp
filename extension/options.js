// Render non-sensitive installation diagnostics without exposing the pairing token.

const status = document.getElementById("status");

/** Display the configured bridge port range and build identifier. */
async function renderStatus() {
  try {
    const response = await fetch(chrome.runtime.getURL("pairing.json"), { cache: "no-store" });
    const config = await response.json();
    const end = Number(config.base_port) + Number(config.pool_size) - 1;
    status.className = "ok";
    status.textContent = `Configured for ports ${config.base_port}–${end}; build ${config.build_id}.`;
  } catch (error) {
    status.className = "error";
    status.textContent = `Pairing configuration is unavailable: ${error}`;
  }
}

void renderStatus();
