# AStock Social Capture

## Install once

1. Open `chrome://extensions` in the Chrome profile that is already signed in to the two sites.
2. Enable Developer mode.
3. Choose **Load unpacked** and select this directory.
4. Open the extension's **Details → Extension options** page.
5. Obtain the local bridge token with `uv run astock social-capture-token`, paste it into the options page, and save.

The extension opens inactive background tabs for search pages, extracts result cards, and sends the structured capture to the local AStock bridge. It does not read the browser cookie database or export cookie values.

## Runtime

The local bridge binds only to `127.0.0.1`. The extension polls while Chrome is running; search requests are processed only while the AStock capture session is active. The search result snapshots and normalized leads are persisted by AStock's existing evidence store.
