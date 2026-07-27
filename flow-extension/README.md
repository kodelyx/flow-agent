# Flow Agent Chrome Extension

Chrome bridge for [kodelyx/flow-agent](https://github.com/kodelyx/flow-agent). It connects a logged-in Google Flow tab to the local Flow Agent backend.

## Features

- Live backend health and Flow credit status
- Quick image and video generation from the popup
- Nano Banana 2 (`gem_pix_2`) as the default image model
- Model, aspect ratio, and video duration controls

## Install

1. Start the backend with `flow serve`.
2. Open `chrome://extensions` and enable **Developer mode**.
3. Click **Load unpacked** and select this `flow-extension` folder.
4. Open <https://labs.google/fx/tools/flow>, sign in, and keep the tab open.
5. The extension shows connected when the backend and Flow session are ready.

Main documentation: [Flow Agent README](../README.md)
