"""API Gateway HTTP API v2 -> ASGI. Configure gateway JWT scopes in deployment."""

from mangum import Mangum

from src.Sentiment_analysis.api.app import app

handler = Mangum(app, lifespan="off")
