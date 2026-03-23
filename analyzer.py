import json
import re

def analyze_sentiment(text, asset):
    """
    Analyzes sentiment using simple, robust keyword matching 
    instead of external APIs.
    """
    text = text.lower()
    
    # Simple bullish/bearish keywords
    bullish = ['good', 'buy', 'growth', 'up', 'win', 'high', 'strong']
    bearish = ['bad', 'sell', 'drop', 'down', 'loss', 'low', 'weak', 'tariff']
    
    score = 0
    for word in bullish:
        if word in text: score += 1
    for word in bearish:
        if word in text: score -= 1
        
    return score
