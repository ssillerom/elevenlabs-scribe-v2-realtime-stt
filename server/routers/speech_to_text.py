"""
Ejemplo de integración de ElevenLabs STT con FastAPI WebSocket.
Router para voice agent con transcripción en tiempo real.
"""
from dotenv import load_dotenv
# Cargar variables de entorno al inicio para asegurar que las API keys estén disponibles
load_dotenv()

import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query, HTTPException, Request
from server.services.elevenlabs import ElevenLabsSTTService


router = APIRouter()

# Logger configurado para este módulo
logger = logging.getLogger(__name__)


@router.websocket("/ws/elevenlabs-voice")
async def elevenlabs_voice_websocket(websocket: WebSocket):
    """
    Endpoint WebSocket principal para el Voice Agent.
    
    Architecture Note:
        Este endpoint implementa un patrón de concurrencia con 3 tareas asíncronas corriendo en paralelo:
        1. receive_audio: Ingesta de audio desde el cliente (Network I/O -> Queue).
        2. process_audio: Lógica de negocio STT (Queue -> External API -> Queue).
        3. send_transcripts: Envío de resultados al cliente (Queue -> Network I/O).
        
        Este diseño evita que el bloqueo en una operación (ej. latencia de red de ElevenLabs)
        afecte a la recepción de audio del cliente, previniendo buffer overflows o cortes de audio.
    """
    await websocket.accept()
    logger.info("Cliente conectado al WebSocket")
    # State Management:
    # Usamos contadores simples para observabilidad básica durante la sesión.
    audio_received = 0
    transcripts_sent = 0

    
    # Communication Channels:
    # Las colas asyncio actúan como buffers entre las tareas productoras y consumidoras.
    audio_queue = asyncio.Queue(maxsize=5)  # Buffer limitado para backpressure
    transcript_queue = asyncio.Queue()
    
    # Synchronization:
    # Evento para coordinar el shutdown limpio de todas las tareas.
    stop_event = asyncio.Event()
    
    # Service Initialization:
    # Instanciamos el servicio STT con configuración optimizada para baja latencia.
    stt_service = ElevenLabsSTTService(
        model_id="scribe_v2_realtime",
        language_code="es",
        sample_rate=16000,
        vad_silence_threshold_secs=0.8,
        vad_threshold=0.4,
        min_speech_duration_ms=100,
        min_silence_duration_ms=100,
        include_timestamps=True,
        logger=logger
    )
    
    # --- Task 1: Ingesta de Audio ---
    async def receive_audio():
        """Consume bytes del WebSocket y alimenta la cola de audio."""
        nonlocal audio_received
        try:
            while not stop_event.is_set():
                # receive_bytes es una operación bloqueante (awaitable)
                audio_data = await websocket.receive_bytes()
                
                audio_received += 1
                
                # Logging muestreado para evitar ruido en logs de producción
                if audio_received % 50 == 0:
                    logger.info(f"📊 Audio recibido: {audio_received} chunks, {len(audio_data)} bytes")
                
                # Backpressure handling implícito: si la cola se llena, put() esperaría (si tuviera maxsize)
                await audio_queue.put(audio_data)
                
        except WebSocketDisconnect:
            logger.info("Cliente desconectado (WebSocketDisconnect)")
            stop_event.set() # Señalizar a otras tareas que deben terminar
        except Exception as e:
            logger.error(f"Error recibiendo audio: {e}")
            stop_event.set()
    
    # --- Task 2: Procesamiento STT ---
    async def process_audio():
        """Orquesta el flujo de datos hacia/desde el servicio de ElevenLabs."""
        try:
            # streaming_recognize maneja su propio bucle interno y conexión
            await stt_service.streaming_recognize(
                audio_queue=audio_queue,
                transcript_queue=transcript_queue,
                stop_event=stop_event
            )
        except Exception as e:
            logger.error(f"Error en servicio STT: {e}")
            stop_event.set()
    
    # --- Task 3: Envío de Respuestas ---
    async def send_transcripts():
        """Consume resultados del STT y los envía de vuelta al cliente."""
        nonlocal transcripts_sent
        try:
            while not stop_event.is_set():
                try:
                    # wait_for permite verificar stop_event periódicamente incluso si la cola está vacía
                    transcript_data = await asyncio.wait_for(transcript_queue.get(), timeout=0.5)
                    
                    # Serialización JSON y envío
                    await websocket.send_json(transcript_data)
                    transcripts_sent += 1
                    logger.info(f"✅ Transcripción #{transcripts_sent} enviada: {transcript_data}")
                    
                except asyncio.TimeoutError:
                    continue # Loop idle
                    
        except Exception as e:
            logger.error(f"Error enviando transcripciones: {e}")
            stop_event.set()
    
    # Execution:
    # Lanzamos las 3 tareas concurrentemente. gather() esperará a que todas terminen.
    # Como usamos stop_event, si una falla o termina, las otras deberían recibir la señal y salir.
    try:
        await asyncio.gather(
            receive_audio(),
            process_audio(),
            send_transcripts()
        )
    except Exception as e:
        logger.error(f"Error no manejado en ciclo de vida del WebSocket: {e}")
    finally:
        # Cleanup:
        # Aseguramos que el evento de parada esté seteado para liberar cualquier tarea pendiente
        stop_event.set()
        
        logger.info(f"📊 Sesión finalizada - Audio chunks: {audio_received}, Transcripciones: {transcripts_sent}")
        
        # Cierre defensivo del WebSocket
        try:
            await websocket.close()
        except RuntimeError:
            # RuntimeError es común si el socket ya estaba cerrado por el cliente
            pass
        except Exception as e:
            logger.warning(f"Error cerrando WebSocket: {e}")
        
        logger.info("Recursos liberados")
