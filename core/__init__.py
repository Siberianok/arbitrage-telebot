"""Core domain package.

La migración es gradual: el código fuente continúa en `arbitrage_telebot.py`
y se expone aquí como API estable por módulo.
"""

from .models import *
from .opportunity_builders import *
from .scoring import *
