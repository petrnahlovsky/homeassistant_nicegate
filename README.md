# Nice IT4WIFI gate for Home Assistant

Local control of Nice gate and garage door actuators connected through the **IT4WIFI** module (NHK protocol).
Works with the current **MyNice** app and current Home Assistant versions.

## Features

- Gate entity (cover) with open, close and stop, including live states opening, closing, open and closed
- State changes are pushed by the module, so automations react also to remote controls and the MyNice app
- Buttons for T4 commands: step by step, partial opening 1–3 and courtesy light (unsupported commands become unavailable)
- Setup with your MyNice account, no pairing code and no user approval needed
- Local communication after setup, the cloud is used only once to load the gate credentials

## Installation

Add this repository to HACS as a custom repository (type *Integration*) or use the button below, then restart Home Assistant.

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=petrnahlovsky&repository=homeassistant_nicegate&category=integration)

## Configuration

*Settings → Devices & services → Add integration → Nice IT4WIFI gate*

| Field | Value |
|---|---|
| Host | IP address of the IT4WIFI module (a DHCP reservation is recommended) |
| MAC address | MAC of the IT4WIFI, e.g. `00:0B:6C:xx:xx:xx` |
| MyNice e-mail / password | Your MyNice account. The password is not stored. |
| Username | Leave empty. Used only for the legacy pairing flow. |

A dedicated MyNice account invited to the gate is recommended for Home Assistant.

### Legacy pairing (without MyNice account)

Leave the MyNice fields empty and enter the setup code from the module label (`999-99-999`).
The new user then has to be approved in the mobile app. The current MyNice app does not show such requests, so this path works only with the old MyNice Welcome app.

## Changes compared to the original project

- TLS compatibility with Python 3.13+ / OpenSSL 3 (legacy renegotiation, 1024-bit DH, TLS 1.2 only)
- Setup via MyNice account (credentials of app users, direct `CONNECT` with controller ID)
- T4 command buttons and a common device for all entities

## Credits and license

Based on [homeassistant_nicegate](https://github.com/PatrikTrestik/homeassistant_nicegate) by Patrik Trestík.
Protocol details were cross-checked with the openHAB MyNice binding.
Licensed under the Apache License 2.0, see [LICENSE](LICENSE).
