import random
from typing import List, Dict

MOCK_VINS = [
    "1HGCM82633A004352",
    "2FMDK3JC1BBA12345",
    "WDBJF65JX1B123456",
    "JM1BK32F771234567",
]


def poll_tickets(limit: int = 5) -> List[Dict[str, str]]:
    tickets = []
    for i in range(min(limit, len(MOCK_VINS))):
        vin = random.choice(MOCK_VINS)
        tickets.append(
            {
                "vin": vin,
                "make": random.choice(["Honda", "Ford", "Mercedes", "Mazda"]),
                "model": random.choice(["Civic", "Escape", "C300", "CX-5"]),
                "status": random.choice(["Connected", "Pending", "In Automation", "Waiting"]),
            }
        )
    return tickets
