"""Launch X431 brand groups for UI selection and Local Diagnose search terms.

Full diagnostic workflows are implemented for VAG first; other brands share the
same Intelligent Diagnose → AutoDetect Result entry path on EURO LINK.
"""

from __future__ import annotations

from typing import Dict, Sequence, Tuple

# Brands with end-to-end workflow in vag_workflows (scan, service reset, etc.).
VAG_FULL_WORKFLOW: frozenset[str] = frozenset(
    {"Volkswagen", "Audi", "SEAT", "Skoda", "Cupra"}
)

# Oil Maintenance Reset via Common Special Function (SGW OK dialogs).
FCA_OIL_RESET: frozenset[str] = frozenset(
    {"Fiat", "Alfa Romeo", "Lancia", "Abarth", "Jeep", "Chrysler"}
)

# After Diagnostic: Show Menu → Automatically Search → YES → YES → tap model.
RENAULT_AUTO_SEARCH: frozenset[str] = frozenset({"Renault", "Dacia"})

# UI brand groups (manufacturer families on EURO LINK).
BRAND_GROUPS: Dict[str, Tuple[str, ...]] = {
    "VAG (full workflow)": (
        "Volkswagen",
        "Audi",
        "SEAT",
        "Skoda",
        "Cupra",
    ),
    "PSA / Stellantis (FCA oil reset)": (
        "Peugeot",
        "Citroen",
        "Opel",
        "DS",
        "Fiat",
        "Alfa Romeo",
        "Lancia",
        "Abarth",
        "Jeep",
        "Chrysler",
    ),
    "Mercedes-Benz": (
        "Mercedes-Benz",
        "Smart",
    ),
    "Ford": (
        "Ford",
        "Ford (Europe)",
        "Lincoln",
    ),
    "Renault / Dacia": (
        "Renault",
        "Dacia",
    ),
    "Toyota / Lexus": (
        "Toyota",
        "Lexus",
    ),
    "Hyundai / Kia / Genesis": (
        "Hyundai",
        "Kia",
        "Genesis",
    ),
    "BMW Group": (
        "BMW",
        "Mini",
        "Rolls-Royce",
    ),
    "Nissan / Infiniti": (
        "Nissan",
        "Infiniti",
    ),
    "Honda / Acura": (
        "Honda",
        "Acura",
    ),
    "Other European": (
        "Volvo",
        "Polestar",
        "Jaguar",
        "Land Rover",
        "Porsche",
        "Bentley",
        "Lamborghini",
        "Maserati",
        "Iveco",
    ),
    "Other Asian / US": (
        "Mazda",
        "Subaru",
        "Mitsubishi",
        "Suzuki",
        "Isuzu",
        "Chevrolet",
        "GMC",
        "Cadillac",
        "Buick",
    ),
}

# Local Diagnose search / tile labels (EURO LINK brand grid).
LOCAL_DIAGNOSE_SEARCH: Dict[str, Sequence[str]] = {
    # VAG
    "Volkswagen": ("VW", "Volkswagen"),
    "Audi": ("Audi",),
    "SEAT": ("SEAT", "Seat"),
    "Skoda": ("Skoda", "Škoda", "SKODA"),
    "Cupra": ("Cupra", "CUPRA"),
    # PSA / Stellantis
    "Peugeot": ("Peugeot",),
    "Citroen": ("Citroen", "Citroën"),
    "Opel": ("Opel", "Vauxhall"),
    "DS": ("DS",),
    "Fiat": ("Fiat",),
    "Alfa Romeo": ("Alfa Romeo", "Alfa"),
    "Lancia": ("Lancia",),
    "Abarth": ("Abarth",),
    "Jeep": ("Jeep",),
    "Chrysler": ("Chrysler",),
    # Mercedes
    "Mercedes-Benz": ("Mercedes", "Mercedes-Benz", "Benz"),
    "Smart": ("Smart",),
    # Ford
    "Ford": ("Ford",),
    "Ford (Europe)": ("Ford", "Ford Europe"),
    "Lincoln": ("Lincoln",),
    # Renault
    "Renault": ("Renault",),
    "Dacia": ("Dacia",),
    # Toyota
    "Toyota": ("Toyota",),
    "Lexus": ("Lexus",),
    # Hyundai group
    "Hyundai": ("Hyundai",),
    "Kia": ("Kia",),
    "Genesis": ("Genesis",),
    # BMW
    "BMW": ("BMW",),
    "Mini": ("Mini", "MINI"),
    "Rolls-Royce": ("Rolls-Royce", "Rolls Royce"),
    # Nissan
    "Nissan": ("Nissan",),
    "Infiniti": ("Infiniti",),
    # Honda
    "Honda": ("Honda",),
    "Acura": ("Acura",),
    # Other
    "Volvo": ("Volvo",),
    "Polestar": ("Polestar",),
    "Jaguar": ("Jaguar",),
    "Land Rover": ("Land Rover", "LandRover"),
    "Porsche": ("Porsche",),
    "Bentley": ("Bentley",),
    "Lamborghini": ("Lamborghini", "Lamborghini"),
    "Maserati": ("Maserati",),
    "Iveco": ("Iveco",),
    "Mazda": ("Mazda",),
    "Subaru": ("Subaru",),
    "Mitsubishi": ("Mitsubishi",),
    "Suzuki": ("Suzuki",),
    "Isuzu": ("Isuzu",),
    "Chevrolet": ("Chevrolet", "Chevy"),
    "GMC": ("GMC",),
    "Cadillac": ("Cadillac",),
    "Buick": ("Buick",),
}


def brand_group_names() -> Tuple[str, ...]:
    return tuple(BRAND_GROUPS.keys())


def brands_in_group(group: str) -> Tuple[str, ...]:
    return BRAND_GROUPS.get(group) or ()


def all_brands_flat() -> Tuple[str, ...]:
    out: list[str] = []
    seen: set[str] = set()
    for brands in BRAND_GROUPS.values():
        for b in brands:
            if b not in seen:
                seen.add(b)
                out.append(b)
    return tuple(out)


def has_full_workflow(brand: str) -> bool:
    return brand.strip() in VAG_FULL_WORKFLOW


def has_fca_oil_reset(brand: str) -> bool:
    return brand.strip() in FCA_OIL_RESET


def has_renault_auto_search(brand: str) -> bool:
    return brand.strip() in RENAULT_AUTO_SEARCH


def local_diagnose_search_terms(brand: str) -> Sequence[str]:
    key = brand.strip()
    if key in LOCAL_DIAGNOSE_SEARCH:
        return LOCAL_DIAGNOSE_SEARCH[key]
    if key.title() in LOCAL_DIAGNOSE_SEARCH:
        return LOCAL_DIAGNOSE_SEARCH[key.title()]
    # Default: search by display name on Local Diagnose grid.
    return (key,)
