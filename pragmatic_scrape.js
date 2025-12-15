const puppeteer = require('puppeteer');

const fs = require('fs').promises;

// --- Configuration ---

const COOKIES_FILE_PATH = './duel.com.cookies.json';

const CASINO_LOBBY_URL = 'https://duel.com/casino/games/837/ppl_blackjack_lobby';

const SEARCH_QUERY = 'Blackjack 43'; // e.g. "Blackjack 9", "Blackjack 23", "Speedblackjack 42"

const HEADLESS_MODE = true;

const INTERCEPT_TIMEOUT_MS = 60000;

/**
 * Capture WebSocket URLs across ALL targets (main page, OOPIFs, workers).
 * Resolves with the first URL that matches the predicate.
 */
async function captureAllWebSockets(browser, predicate = () => true, { timeoutMs = 60000 } = {}) {
  const sessions = new Set();
  let resolved = false;

  let resolvePromise, rejectPromise;
  const wsPromise = new Promise((res, rej) => {
    resolvePromise = res;
    rejectPromise = rej;
  });

  const cleanup = () => {
    browser.off('targetcreated', onTargetCreated);
    for (const s of sessions) {
      try { s.detach(); } catch {}
    }
    sessions.clear();
  };

  const handleWSUrl = (url, source) => {
    // Uncomment to debug all observed WebSocket URLs
    // console.log(`[${source}] WS observed: ${url}`);
    if (!resolved && predicate(url)) {
      resolved = true;
      cleanup();
      resolvePromise(url);
    }
  };

  const attachToTarget = async (target) => {
    const type = target.type();
    // We care about: main pages, OOPIFs ("other"), and workers of all kinds
    if (!['page', 'other', 'worker', 'service_worker', 'shared_worker'].includes(type)) return;

    try {
      const session = await target.createCDPSession();
      sessions.add(session);
      await session.send('Network.enable');

      // Fires when a WebSocket is created.
      session.on('Network.webSocketCreated', ({ url }) => handleWSUrl(url, `${type}:webSocketCreated`));
      // Also capture handshake request if available (belt and suspenders).
      session.on('Network.webSocketWillSendHandshakeRequest', ({ request: { url } }) => handleWSUrl(url, `${type}:handshake`));

      // Clean session when target closes
      target.on('close', () => {
        try { session.detach(); } catch {}
        sessions.delete(session);
      });
    } catch (err) {
      // Silently ignore attach errors
    }
  };

  const onTargetCreated = (target) => {
    // Attach to future OOPIFs/workers created after clicks
    attachToTarget(target);
  };

  // Attach to already-existing targets first
  await Promise.all(browser.targets().map(attachToTarget));
  // Then keep listening for future targets
  browser.on('targetcreated', onTargetCreated);

  // Fallback: also listen on the top-level page's request events (in case it's not OOPIF/Worker)
  // Caller can optionally add their own page-level listener, but this keeps things robust.

  // Timeout
  const timer = setTimeout(() => {
    if (!resolved) {
      cleanup();
      rejectPromise(new Error(`WebSocket URL interception timed out after ${timeoutMs} ms.`));
    }
  }, timeoutMs);

  wsPromise.finally(() => clearTimeout(timer));
  return wsPromise;
}

async function getWebSocketUrl() {
  let browser;
  console.log('--- Starting URL Capture Process ---');

  try {
    browser = await puppeteer.launch({ headless: HEADLESS_MODE });
    const page = await browser.newPage();
    await page.setViewport({ width: 1920, height: 1080 });

    // Parent-page fallback listener (will not see OOPIF/Worker WS, but harmless to keep)
    page.on('request', (request) => {
      if (request.resourceType && request.resourceType() === 'websocket') {
        const url = request.url();
        // console.log('[parent page] WS seen:', url);
      }
    });

    // Start capturing WebSockets across all targets BEFORE any navigation/clicks
    const wsPromise = captureAllWebSockets(
      browser,
      // Match the PPL provider sockets you showed (contains wss:// and tableId=)
      (url) => typeof url === 'string' && url.startsWith('wss://') && url.includes('tableId='),
      { timeoutMs: INTERCEPT_TIMEOUT_MS }
    );

    console.log(`Loading cookies from: ${COOKIES_FILE_PATH}`);
    const cookiesString = await fs.readFile(COOKIES_FILE_PATH);
    const cookies = JSON.parse(cookiesString);
    if (Array.isArray(cookies) && cookies.length > 0) {
      await page.setCookie(...cookies);
      console.log('Cookies loaded successfully.');
    } else {
      console.log('Cookie file did not contain an array of cookies. Proceeding without cookies.');
    }

    console.log(`Navigating to: ${CASINO_LOBBY_URL}`);
    await page.goto(CASINO_LOBBY_URL, { waitUntil: 'networkidle2' });
    console.log('Successfully navigated to the live casino page.');

    console.log('Waiting for the game lobby iframe to load...');
    const iframeSelector = '#app iframe';
    await page.waitForSelector(iframeSelector, { timeout: 30000 });
    const iframeElement = await page.$(iframeSelector);
    const frame = await iframeElement.contentFrame();
    if (!frame) throw new Error('Could not get the content frame of the iframe.');
    console.log('Successfully switched context to the game lobby iframe.');

    const searchInputSelector = '#searchInput__id';
    await frame.waitForSelector(searchInputSelector, { visible: true, timeout: 30000 });
    console.log('Search input is now visible inside the iframe.');

    await frame.click(searchInputSelector);
    console.log('Clicked on the search input.');

    await frame.type(searchInputSelector, SEARCH_QUERY, { delay: 100 });
    console.log(`Typing search query: "${SEARCH_QUERY}"`);
 
    // Wait for the target tile to appear and click it via evaluate (XPath inside the iframe)
    const tableXPath = `//span[@data-testid="tile-container-title" and contains(text(), "${SEARCH_QUERY}")]`;

    console.log('Waiting for the table tile XPath to appear...');
    await frame.waitForFunction(
      (xpath) =>
        document.evaluate(xpath, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue,
      { timeout: 15000 },
      tableXPath
    );
    console.log(`Found the "${SEARCH_QUERY}" table tile.`);

    await frame.evaluate((xpath) => {
      const element = document.evaluate(xpath, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
      if (element) {
        element.click();
      } else {
        throw new Error('Tile element disappeared before click.');
      }
    }, tableXPath);
    console.log(`Successfully clicked on "${SEARCH_QUERY}"!`);

    console.log('Waiting for the game to load and the WebSocket URL to be captured...');
    const webSocketUrl = await wsPromise; // resolves on first matching WS across all targets
    console.log('INTERCEPTED WEBSOCKET URL:', webSocketUrl);

    return webSocketUrl;

  } catch (error) {
    console.error('An error occurred during the URL capture process:', error);
    return null;
  } finally {
    if (browser) {
      await browser.close();
      console.log('--- Browser closed. Process finished. ---');
    }
  }
}

(async () => {
  const url = await getWebSocketUrl();
  if (url) {
    console.log('\n✅ SUCCESS: Captured URL:', url);
  } else {
    console.log('\n❌ FAILURE: Could not capture the WebSocket URL.');
  }
})();
