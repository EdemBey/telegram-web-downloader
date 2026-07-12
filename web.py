import os
import json
import asyncio
import time
import re
import smbclient
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

load_dotenv(override=True)

app = FastAPI(title="Telegram Web Downloader")

# Directories
DOWNLOADS_DIR = os.getenv("DOWNLOADS_DIR", "downloads")
SESSION_DIR = os.getenv("SESSION_DIR", ".")
os.makedirs(DOWNLOADS_DIR, exist_ok=True)
os.makedirs(SESSION_DIR, exist_ok=True)

SETTINGS_FILE = os.path.join(SESSION_DIR, "settings.json")

def load_settings():
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "storage_type": "local",
        "local_path": DOWNLOADS_DIR,
        "smb_server": "",
        "smb_share": "",
        "smb_path": "",
        "smb_user": "",
        "smb_pass": ""
    }

def save_settings(settings):
    with open(SETTINGS_FILE, "w") as f:
        json.dump(settings, f)

# Telethon config
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")

session_path = os.path.join(SESSION_DIR, "session")
client = TelegramClient(session_path, API_ID, API_HASH)

DOWNLOAD_SEMAPHORE = asyncio.Semaphore(3)

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# State
auth_state = {
    "is_authorized": False,
    "phone_hash": None,
    "phone": None
}

active_downloads = {} # msg_id -> Task
queue_info = {}       # msg_id -> {url, status, total_mb, downloaded_mb, speed_mbps, eta, ext}
speed_history = {}    # msg_id -> list of (time, downloaded_mb)

@app.on_event("startup")
async def startup_event():
    if API_ID == 0 or not API_HASH:
        print("CRITICAL: API_ID or API_HASH not set!")
        return
    await client.connect()
    auth_state["is_authorized"] = await client.is_user_authorized()

@app.on_event("shutdown")
async def shutdown_event():
    await client.disconnect()

@app.get("/", response_class=HTMLResponse)
async def get_index(request: Request):
    return templates.TemplateResponse(
        request=request, 
        name="index.html", 
        context={"authorized": auth_state["is_authorized"]}
    )

class PhoneRequest(BaseModel):
    phone: str

@app.post("/api/auth/send_code")
async def send_code(req: PhoneRequest):
    try:
        res = await client.send_code_request(req.phone)
        auth_state["phone_hash"] = res.phone_code_hash
        auth_state["phone"] = req.phone
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

class CodeRequest(BaseModel):
    code: str

@app.post("/api/auth/verify_code")
async def verify_code(req: CodeRequest):
    try:
        await client.sign_in(auth_state["phone"], req.code, phone_code_hash=auth_state["phone_hash"])
        auth_state["is_authorized"] = True
        return {"status": "ok"}
    except SessionPasswordNeededError:
        raise HTTPException(status_code=400, detail="2FA Password required (not supported in this simple UI yet)")
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

class URLRequest(BaseModel):
    url: str
    overwrite: bool = False

async def download_worker(message, msg_id, url):
    file_size = getattr(message.file, "size", 0)
    total_mb = file_size / (1024 * 1024)
    ext = getattr(message.file, "ext", ".mp4") or ".mp4"
    file_name = None
    if message.file and getattr(message.file, "name", None):
        safe_name = re.sub(r'[\\/*?:"<>|]', "", message.file.name)
        base_name, _ = os.path.splitext(safe_name)
        if base_name:
            file_name = f"{base_name}_{msg_id}{ext}"
            
    if not file_name:
        file_name = f"video_{msg_id}{ext}"
    
    queue_info[msg_id] = {
        "url": url,
        "status": "queue",
        "total_mb": total_mb,
        "downloaded_mb": 0.0,
        "speed_mbps": 0.0,
        "eta": "--:--",
        "ext": ext,
        "storage_type": "",
        "output_path": "",
        "file_name": file_name,
        "created_at": time.time()
    }
    speed_history[msg_id] = []
    
    settings = load_settings()
    file_obj = None
    output_path = ""
    
    if settings.get("storage_type") == "smb":
        queue_info[msg_id]["storage_type"] = "smb"
        server = settings.get("smb_server", "")
        share = settings.get("smb_share", "")
        smb_path = settings.get("smb_path", "")
        user = settings.get("smb_user", "")
        password = settings.get("smb_pass", "")
        
        try:
            smbclient.register_session(server, username=user, password=password)
            share = share.strip("\\/")
            smb_path = smb_path.strip("\\/")
            full_smb_dir = fr"\\{server}\{share}\{smb_path}" if smb_path else fr"\\{server}\{share}"
            if smb_path:
                try:
                    smbclient.makedirs(full_smb_dir, exist_ok=True)
                except Exception:
                    pass
            output_path = fr"{full_smb_dir}\{file_name}"
            queue_info[msg_id]["output_path"] = output_path
            
            for attempt in range(3):
                try:
                    file_obj = smbclient.open_file(output_path, mode="wb", username=user, password=password)
                    break
                except Exception as open_e:
                    err_str = str(open_e).upper()
                    if ("STATUS_DELETE_PENDING" in err_str or "0XC0000056" in err_str or "STATUS_SHARING_VIOLATION" in err_str or "0XC0000043" in err_str) and attempt < 2:
                        await asyncio.sleep(2.0)
                    else:
                        raise open_e
        except Exception as e:
            err_msg = str(e).upper()
            if "STATUS_SHARING_VIOLATION" in err_msg or "0XC0000043" in err_msg:
                queue_info[msg_id]["status"] = "error: File is open in another program"
            else:
                queue_info[msg_id]["status"] = f"error: SMB connect failed - {str(e)}"
            if msg_id in active_downloads:
                del active_downloads[msg_id]
            return
    else:
        queue_info[msg_id]["storage_type"] = "local"
        local_dir = settings.get("local_path") or DOWNLOADS_DIR
        os.makedirs(local_dir, exist_ok=True)
        output_path = os.path.join(local_dir, file_name)
        queue_info[msg_id]["output_path"] = output_path
        file_obj = output_path
    
    def progress_callback(received, total):
        if total:
            now = time.time()
            recv_mb = received / (1024*1024)
            queue_info[msg_id]["status"] = "downloading"
            queue_info[msg_id]["downloaded_mb"] = recv_mb
            
            # calculate speed
            history = speed_history[msg_id]
            history.append((now, recv_mb))
            # keep last 5 seconds
            history = [(t, v) for t, v in history if now - t <= 5.0]
            speed_history[msg_id] = history
            
            speed = 0.0
            if len(history) > 1:
                dt = history[-1][0] - history[0][0]
                dv = history[-1][1] - history[0][1]
                if dt > 0: speed = dv / dt
            
            queue_info[msg_id]["speed_mbps"] = speed
            
            eta_str = "--:--"
            if speed > 0:
                rem = total_mb - recv_mb
                eta_sec = rem / speed
                mins = int(eta_sec // 60)
                secs = int(eta_sec % 60)
                eta_str = f"{mins:02d}:{secs:02d}"
            
            queue_info[msg_id]["eta"] = eta_str

    async with DOWNLOAD_SEMAPHORE:
        try:
            await message.download_media(file=file_obj, progress_callback=progress_callback)
            queue_info[msg_id]["status"] = "done"
            queue_info[msg_id]["downloaded_mb"] = total_mb
            queue_info[msg_id]["speed_mbps"] = 0.0
            queue_info[msg_id]["eta"] = "00:00"
        except asyncio.CancelledError:
            queue_info[msg_id]["status"] = "cancelled"
        except Exception as e:
            queue_info[msg_id]["status"] = f"error: {str(e)}"
        finally:
            if settings.get("storage_type") == "smb" and hasattr(file_obj, "close"):
                try:
                    file_obj.close()
                except:
                    pass
            if msg_id in active_downloads:
                del active_downloads[msg_id]

class SettingsRequest(BaseModel):
    storage_type: str
    local_path: str = ""
    smb_server: str = ""
    smb_share: str = ""
    smb_path: str = ""
    smb_user: str = ""
    smb_pass: str = ""

@app.get("/api/settings")
async def get_settings_api():
    return load_settings()

@app.post("/api/settings")
async def update_settings_api(req: SettingsRequest):
    settings = req.dict()
    save_settings(settings)
    return {"status": "ok"}

@app.post("/api/downloads")
async def add_download(req: URLRequest):
    url = req.url.strip()
    parts = url.rstrip('/').split('/')
    try:
        msg_id = int(parts[-1])
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid URL format")
        
    if 'c/' in url:
        idx = parts.index('c')
        entity = int("-100" + parts[idx + 1])
    else:
        try:
            idx = parts.index('t.me')
            entity = parts[idx + 1]
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid Telegram URL")

    try:
        message = await client.get_messages(entity, ids=msg_id)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error fetching msg: {e}")
        
    if not message or not message.media:
        raise HTTPException(status_code=404, detail="Video not found or deleted")
        
    if msg_id in queue_info and queue_info[msg_id]["status"] in ["queue", "downloading"]:
        return {"status": "already_queued"}

    ext = getattr(message.file, "ext", ".mp4") or ".mp4"
    file_name = None
    if message.file and getattr(message.file, "name", None):
        safe_name = re.sub(r'[\\/*?:"<>|]', "", message.file.name)
        base_name, _ = os.path.splitext(safe_name)
        if base_name:
            file_name = f"{base_name}_{msg_id}{ext}"
            
    if not file_name:
        file_name = f"video_{msg_id}{ext}"

    if not req.overwrite:
        settings = load_settings()
        file_exists = False
        
        if settings.get("storage_type") == "smb":
            server = settings.get("smb_server", "")
            share = settings.get("smb_share", "")
            smb_path = settings.get("smb_path", "")
            user = settings.get("smb_user", "")
            password = settings.get("smb_pass", "")
            
            try:
                smbclient.register_session(server, username=user, password=password)
                share = share.strip("\\/")
                smb_path = smb_path.strip("\\/")
                full_smb_dir = fr"\\{server}\{share}\{smb_path}" if smb_path else fr"\\{server}\{share}"
                output_path = fr"{full_smb_dir}\{file_name}"
                
                try:
                    smbclient.stat(output_path, username=user, password=password)
                    file_exists = True
                except Exception:
                    pass
            except Exception:
                pass
        else:
            local_dir = settings.get("local_path") or DOWNLOADS_DIR
            output_path = os.path.join(local_dir, file_name)
            file_exists = os.path.exists(output_path)
            
        if file_exists:
            return {"status": "file_exists", "msg_id": msg_id, "file_name": file_name}

    if msg_id in active_downloads:
        old_task = active_downloads[msg_id]
        if not old_task.done():
            old_task.cancel()
            try:
                await asyncio.wait_for(old_task, timeout=5.0)
            except Exception:
                pass

    task = asyncio.create_task(download_worker(message, msg_id, url))
    active_downloads[msg_id] = task
    
    return {"status": "ok", "msg_id": msg_id}

@app.get("/api/downloads")
async def get_downloads():
    return {"downloads": queue_info}

from fastapi.responses import StreamingResponse

@app.get("/api/stream_downloads")
async def stream_downloads(request: Request):
    async def event_generator():
        last_state_str = ""
        while True:
            if await request.is_disconnected():
                break
            
            current_state_str = json.dumps(queue_info)
            if current_state_str != last_state_str:
                yield f"data: {current_state_str}\n\n"
                last_state_str = current_state_str
                
            await asyncio.sleep(0.5)

    return StreamingResponse(event_generator(), media_type="text/event-stream")

@app.delete("/api/downloads/{msg_id}")
async def cancel_download(msg_id: int):
    if msg_id in active_downloads:
        task = active_downloads[msg_id]
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except Exception:
            pass
    if msg_id in queue_info:
        queue_info[msg_id]["status"] = "cancelled"
    return {"status": "ok"}

class RemoveRequest(BaseModel):
    delete_file: bool = False

@app.post("/api/downloads/{msg_id}/remove")
async def remove_download(msg_id: int, req: RemoveRequest):
    if msg_id in active_downloads:
        task = active_downloads[msg_id]
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except Exception:
            pass
        active_downloads.pop(msg_id, None)
        
    info = queue_info.get(msg_id)
    if info:
        if req.delete_file:
            path = info.get("output_path")
            storage_type = info.get("storage_type")
            
            # small delay to ensure OS released the file handle
            await asyncio.sleep(0.5)
            
            if storage_type == "local" and path and os.path.exists(path):
                try:
                    os.remove(path)
                except Exception as e:
                    print(f"Error deleting file {path}: {e}")
                    raise HTTPException(status_code=400, detail=f"Cannot delete local file: {str(e)}")
            elif storage_type == "smb" and path:
                try:
                    settings = load_settings()
                    user = settings.get("smb_user", "")
                    password = settings.get("smb_pass", "")
                    server = settings.get("smb_server", "")
                    smbclient.register_session(server, username=user, password=password)
                    try:
                        smbclient.remove(path, username=user, password=password)
                    except Exception as e:
                        err_msg = str(e).upper()
                        if "STATUS_OBJECT_NAME_NOT_FOUND" in err_msg or "0XC0000034" in err_msg or "OBJECT_NAME_NOT_FOUND" in err_msg:
                            pass # already deleted
                        elif "STATUS_SHARING_VIOLATION" in err_msg or "0XC0000043" in err_msg or "STATUS_DELETE_PENDING" in err_msg or "0XC0000056" in err_msg:
                            raise HTTPException(status_code=400, detail="Cannot delete: File is open in another program (Pending Delete)")
                        else:
                            raise HTTPException(status_code=400, detail=f"SMB remove error: {str(e)}")
                    
                    # Verify it's actually deleted
                    try:
                        smbclient.stat(path, username=user, password=password)
                        # If stat succeeds, the file is STILL THERE (e.g. silently locked)
                        raise HTTPException(status_code=400, detail="Cannot delete: File is locked by another program")
                    except HTTPException:
                        raise
                    except Exception as stat_e:
                        stat_err = str(stat_e).upper()
                        if "STATUS_DELETE_PENDING" in stat_err or "0XC0000056" in stat_err:
                            raise HTTPException(status_code=400, detail="Cannot delete: File is open in another program (Pending Delete)")
                        # If Object Name Not Found, it's successfully deleted
                        
                except HTTPException:
                    raise
                except Exception as e:
                    print(f"Error deleting smb file {path}: {e}")
                    raise HTTPException(status_code=400, detail=f"Error deleting smb file: {str(e)}")
        del queue_info[msg_id]
        if msg_id in speed_history:
            del speed_history[msg_id]
    return {"status": "ok"}


@app.get("/api/download_file/{msg_id}")
async def download_file(msg_id: int):
    info = queue_info.get(msg_id)
    if not info:
        raise HTTPException(status_code=404, detail="Download not found")
    if info.get("status") != "done":
        raise HTTPException(status_code=400, detail="Download not finished")
    if info.get("storage_type") != "local":
        raise HTTPException(status_code=400, detail="Not a local file")
    
    path = info.get("output_path", "")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="File deleted or missing")
        
    filename = os.path.basename(path)
    return FileResponse(path, filename=filename)
