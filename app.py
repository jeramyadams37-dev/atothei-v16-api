import eventlet
eventlet.monkey_patch()

from fastapi import FastAPI

app = FastAPI(title="Atothei v16 API", version="1.0.0")

@app.get("/")
async def root():
    return {
        "status": "online", 
        "message": "Atothei v16 FastAPI Container Active", 
        "system": "AuroraOS",
        "routing": "async"
    }
