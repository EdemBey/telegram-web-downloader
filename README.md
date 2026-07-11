# Telegram Web Downloader 🚀

[![Docker Pulls](https://img.shields.io/docker/pulls/edembey/telegram-web-downloader.svg?logo=docker)](https://hub.docker.com/r/edembey/telegram-web-downloader)
[![Docker Image Size](https://img.shields.io/docker/image-size/edembey/telegram-web-downloader/latest?logo=docker)](https://hub.docker.com/r/edembey/telegram-web-downloader)

A sleek, web-based, dockerized application to seamlessly download Telegram videos directly to your local machine or a remote Samba (SMB) network share.

![UI Screenshot](https://raw.githubusercontent.com/EdemBey/telegram-web-downloader/main/image-1.png)

## ✨ Features
- **Web UI:** Modern, responsive glassmorphism interface.
- **Direct Telegram Downloads:** Just paste a Telegram video link, and the app handles the rest.
- **Samba (SMB) Integration:** Download files directly to a NAS or Windows shared folder without wasting local container space! The app will automatically create missing directories.
- **Local Storage Fallback:** Option to download locally with a simple "Save to PC ⬇" button from the browser.
- **Live Progress:** Real-time speed (MB/s), ETA, and progress bars.
- **Smart Queue:** Cancel, restart, and copy links for active and past downloads.
- **Multi-Architecture:** Fully supports `amd64` (x86) and `arm64` (Apple Silicon / Raspberry Pi).

## 🐳 Quick Start (Docker)

The easiest way to get started is using the pre-built Docker image from Docker Hub.

```yaml
services:
  telegram-downloader:
    image: edembey/telegram-web-downloader:latest
    container_name: telegram-web-downloader
    ports:
      - "44321:44321"
    env_file:
      - .env
    volumes:
      - ./data:/app/data
      - ./downloads:/app/downloads
    restart: unless-stopped
```

1. **Get your Telegram API credentials:**
   Go to [my.telegram.org](https://my.telegram.org) (under API development tools) and get your **API_ID** and **API_HASH**.

2. **Create configuration files:**
   Create a `docker-compose.yml` with the content above, and in the same directory create a `.env` file with your credentials:
   ```env
   API_ID=1234567
   API_HASH=your_api_hash_here
   ```
   *(If you are deploying via Portainer, TrueNAS, or Synology UI, simply add `API_ID` and `API_HASH` as environment variables).*

3. Run `docker compose up -d`.
4. Open `http://<your-ip>:44321` in your browser.
5. **Log in:** The first time you open the app, it will ask for your Telegram phone number and a verification code to authorize the session.

## ⚙️ Configuration (Samba & Local Storage)

Click the **Settings (⚙️)** gear icon in the top right of the Web UI to configure where your files go.

### Samba (NAS / Network Share)
Instead of filling up your local drive, you can pipe downloads directly over the network to your NAS!
- **Samba Server IP:** e.g., `192.168.1.100`
- **Share Name:** The name of the SMB share (e.g., `Storage`)
- **Path inside Share:** Optional subfolder (e.g., `downloads/telegram`). *If it doesn't exist, the app will automatically create it!*
- **Username / Password:** Credentials for the SMB share.

### Local Folder
If you prefer to download locally, the files will be saved inside the container's `/app/downloads` folder (which maps to `./downloads` on your host machine). Once a download finishes locally, a **"Save to PC ⬇"** button appears, allowing you to instantly download the file through your browser.
