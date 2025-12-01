from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from server.routers.speech_to_text import router as elevenlabs_router

app = FastAPI(
    title="ElevenLabs Scribe V2 Realtime STT Server",
    description="Transcript in real-time using ElevenLabs Scribe V2 model via WebSocket",
    version="1.0.0",
)

# Añadimos CORS ya que se necesita para poder hacer peticiones
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Incluimos los routers
app.include_router(elevenlabs_router, prefix="/v1/elevenlabs", tags=["ElevenLabs STT"])


@app.get("/healthcheck", response_class=JSONResponse)
async def healthcheck():
    """
    Endpoint para verificar el estado del servidor.

    Returns:
        JSONResponse: Estado del servidor.
    """
    return JSONResponse({"status": 200})


# To run in local uncomment and then run in root this command: python -m server.app

if __name__ == "__main__":
    import logging
    import uvicorn
    
    # Configurar logging detallado
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    # Asegurar que el logger de stt esté en INFO
    logging.getLogger("stt").setLevel(logging.INFO)
    
    uvicorn.run("server.app:app", host="0.0.0.0", port=5050, reload=False)