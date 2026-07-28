# Flow Agent Chrome Extension

Chrome bridge for [kodelyx/flow-agent](https://github.com/kodelyx/flow-agent). It connects a logged-in Google Flow tab to the local Flow Agent backend.

## Features

- Live backend health and Flow credit status
- Premium light-mode side panel opened directly from the extension icon
- Quick image and video generation with persistent history
- Nano Banana 2 as the default image model
- Model, aspect ratio, and video duration controls
- Compact agent monitoring, token refresh, and Flow controls

## Install

1. Start the backend with `flow`.
2. Open `chrome://extensions` and enable **Developer mode**.
3. Click **Load unpacked** and select this `flow-extension` folder.
4. Open <https://labs.google/fx/tools/flow>, sign in, and keep the tab open.
5. Click the extension icon to open Flow Agent in Chrome's side panel.

Main documentation: [Flow Agent README](../README.md)
