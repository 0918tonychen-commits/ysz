from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles 
from fastapi.templating import Jinja2Templates 

# 實例化 FastAPI 應用
app = FastAPI()

# --- 1. 配置靜態檔案目錄 ---
# 讓 FastAPI 知道如何提供 static/ 資料夾中的圖片、CSS 等檔案
app.mount("/static", StaticFiles(directory="static"), name="static")

# --- 2. 配置 HTML 模板目錄 ---
templates = Jinja2Templates(directory="templates")

# --- 網站路由 (路徑) ---

# 根路徑 "/" (首頁) - 顯示 index.html
@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {"request": request} 
    )

# 顯示照片的專用路徑 "/photo"
@app.get("/photo", response_class=HTMLResponse)
async def show_bakery_photo(request: Request):
    # 這裡已替換成您的圖片名稱
    photo_filename = "88888.jpg" 
    
    # photo_url 是圖片在網站上的完整路徑
    photo_url = f"/static/images/{photo_filename}" 
    
    return templates.TemplateResponse(
        "photo.html",
        {"request": request, "photo_url": photo_url}
    )

# 原有的 "/about" 路徑
@app.get("/about")
def about():
    return {"message": "我的第一個 FastAPI 應用"}         