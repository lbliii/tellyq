# TellyQ

A personal TV programmer: describe a mood, then let Cueby, your TV-programming companion, choose and play a mix of familiar favorites and adjacent discoveries.

TellyQ is the app; Cueby is its companion.

## First milestone

Queue and start one real program on the living-room Chromecast through software commands, then verify playback and stop control.

- Service: YouTube.
- Program: [Bob Ross — Autumn Fantasy (season 20, episode 7)](https://www.youtube.com/watch?v=FozIp7Va7dY), from the official channel.
- Controller: a small local Python program using PyChromecast.
- Interface: structured commands for queue, start, status, and stop. Add MCP after the playback connection works.

This repository currently contains the plan and research handoff. No controller has been implemented, no devices have been discovered, and no TV playback has been tested.

Read [the MVP plan](docs/MVP.md) and [research notes](docs/RESEARCH.md) before implementation. An example one-item queue is in [examples/queue.json](examples/queue.json).

## Longer-term direction

Theme-based programming across Netflix, Disney+, Prime Video, and Apple TV, with reliable queue advancement and a balance of comfort viewing and discovery. The initial YouTube experiment proves the playback connection; it does not establish support for those subscription services.
