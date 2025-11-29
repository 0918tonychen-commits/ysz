from fastapi import FastAPI

app = FastAPI()

@app.get("/")
def read_root():
    return {"message": "Hello FastAPI!", "status": "deployed"}

@app.get("/hello/{name}")
def hello(name: str):
    return {"message": f"你好, {name}!"}

@app.get("/about")
def about():
    return {
        "app": "我的第一個 FastAPI 應用",
        "version": "1.0.0",
        "author": "Wilson"
    }
