from fastapi import FastAPI

app = FastAPI()

@app.get("/")
def read_root():
    return {"message": "Hello FastAPI!"}

@app.get("/hello/{name}")
def hello(name: str):
    return {"message": f"你好, {name}!"}
