"""Mock Shopify catalog used until a real Admin API token is configured."""

MOCK_CATALOG = [
    {
        "id": 111,
        "title": "Aurora Pack",
        "handle": "aurora-pack",
        "product_type": "Backpack",
        "vendor": "Nimbus Outfitters",
        "status": "active",
        "tags": "travel, waterproof, backpack",
        "body_html": (
            "<p>The Aurora Pack was made for long trips in bad weather. It has a 32 L main "
            "compartment, a top grab handle, and a roll-top closure that keeps rain out."
            "</p><ul><li>Capacity: 32 liters</li></ul>"
        ),
        "images": [
            {"id": 1, "src": "https://cdn.mockstore.com/aurora-1.jpg", "alt": "Nimbus Outfitters Aurora Pack waterproof travel backpack, three quarter view", "width": 1600, "height": 1600},
            {"id": 2, "src": "https://cdn.mockstore.com/aurora-2.jpg", "alt": "Aurora Pack straps detail", "width": 1600, "height": 1600},
            {"id": 3, "src": "https://cdn.mockstore.com/aurora-3.jpg", "alt": "", "width": 1600, "height": 1600},
        ],
        "faq": [
            {"q": "Is the Aurora Pack waterproof?", "a": "The shell is 500D TPU-coated nylon with a PU rain cover included, rated to withstand sustained rain."},
            {"q": "What are the dimensions?", "a": "28 x 18 x 12 inches, fits standard carry-on."},
        ],
        "metafields": {
            "material": "500D TPU-coated nylon with PU rain cover",
            "dimensions": "28 x 18 x 12 in, 32 L capacity",
            "compatibility": "Fits 15-17 inch laptops; compatible with airline carry-on sizers",
            "intended_use": "Multi-day travel, commuting in rain, hiking",
            "limitations": "Not designed for climbing or over 40 lb loads",
        },
        "reviews": {"count": 812, "avg": 4.6, "source": "yotpo"},
        "variants": [{"id": 901, "sku": "AUR-32-MID-021", "price": "184.00", "inventory": 42}],
    },
    {
        "id": 222,
        "title": "Drift",
        "handle": "drift",
        "product_type": "",
        "vendor": "Nimbus Outfitters",
        "status": "active",
        "tags": "",
        "body_html": (
            "<p>A Jacket. Light, simple, and it goes with everything. You will love the "
            "attention to detail in our new design.</p>"
        ),
        "images": [{"id": 1, "src": "https://cdn.mockstore.com/drift-1.jpg", "alt": "Drift", "width": 1200, "height": 1200}],
        "faq": [],
        "metafields": {},
        "reviews": {"count": 87, "avg": 4.1, "source": "judge.me"},
        "variants": [{"id": 902, "sku": "DRF-000", "price": "129.00", "inventory": 8}],
    },
    {
        "id": 333,
        "title": "Weekender Duffel - 45L - Odor Proof - Olive Drab - W37",
        "handle": "weekender-duffel-45",
        "product_type": "Duffel",
        "vendor": "Nimbus Outfitters",
        "status": "active",
        "tags": "duffel, weekender, travel",
        "body_html": (
            "<p>The Weekender Duffel holds enough for a three day trip. It is a duffel bag "
            "sized 45L with an odor resistant lining and a separate shoe pocket.</p>"
        ),
        "images": [
            {"id": 1, "src": "https://cdn.mockstore.com/wk-1.jpg", "alt": "Nimbus Outfitters Weekender Duffel 45L olive drab side view", "width": 1400, "height": 1400},
            {"id": 2, "src": "https://cdn.mockstore.com/wk-2.jpg", "alt": "Weekender Duffel shoe pocket open", "width": 1400, "height": 1400},
            {"id": 3, "src": "https://cdn.mockstore.com/wk-3.jpg", "alt": "Weekender Duffel packed with clothing", "width": 1400, "height": 1400},
            {"id": 4, "src": "https://cdn.mockstore.com/wk-4.mp4", "alt": "Feature tour video", "width": 1920, "height": 1080},
        ],
        "faq": [{"q": "What are the dimensions?", "a": "26 x 12 x 11 inches. Weights 3.2 lb."}],
        "metafields": {
            "material": "water resistant recycled 900D polyester",
            "dimensions": "26 x 12 x 11 in, 45 L",
            "compatibility": "Carry-on legal for most airlines",
            "intended_use": "Weekend trips, gym, weekend getaways",
        },
        "reviews": {"count": 1502, "avg": 4.4, "source": "yotpo"},
        "variants": [{"id": 903, "sku": "WD-45-OD", "price": "165.00", "inventory": 120}],
    },
    {
        "id": 444,
        "title": "Fog City Rain Jacket",
        "handle": "fog-city-rain-jacket",
        "product_type": "Rain Jacket",
        "vendor": "Nimbus Outfitters",
        "status": "active",
        "tags": "jacket, rain, commuting",
        "body_html": (
            "<p>The Fog City Rain Jacket keeps you dry while walking or biking across town. "
            "It packs into its own chest pocket and weighs eleven ounces.</p><ul>"
            "<li>Material: 2.5 layer waterproof laminate, 20k mm rating</li>"
            "<li>Intended use: city commuting, travel rain shell</li>"
            "<li>Compatibility: fits over a hoodie or blazer</li>"
            "<li>Limitations: not for climbing, not a ski shell</li></ul>"
        ),
        "images": [
            {"id": 1, "src": "https://cdn.mockstore.com/fog-1.jpg", "alt": "Nimbus Outfitters Fog City rain jacket packs into its chest pocket", "width": 1500, "height": 1500},
            {"id": 2, "src": "https://cdn.mockstore.com/fog-2.jpg", "alt": "Fog City rain jacket worn on a bike in rain", "width": 1500, "height": 1500},
            {"id": 3, "src": "https://cdn.mockstore.com/fog-3.jpg", "alt": "Fog City jacket hood and visor detail", "width": 1500, "height": 1500},
            {"id": 4, "src": "https://cdn.mockstore.com/fog-4.jpg", "alt": "Fog City jacket back view cyclist in rain", "width": 1500, "height": 1500},
        ],
        "faq": [
            {"q": "Is this jacket good for biking?", "a": "It is cut for over-the-shoulder coverage on a bike, with reflective rear vents."},
            {"q": "Will it fit over a blazer?", "a": "Sized to layer; large fits over blazers."},
        ],
        "metafields": {
            "material": "2.5 Layer waterproof laminate, 20,000 mm/H2O",
            "dimensions": "11 oz, packs to 0.9 L",
            "compatibility": "Fits over hoodie or blazer",
            "intended_use": "City commuting, travel rain shell",
            "limitations": "Not for climbing or ski use",
        },
        "reviews": {"count": 1221, "avg": 4.7, "source": "judge.me"},
        "variants": [{"id": 904, "sku": "FC-RJ-BLK", "price": "148.00", "inventory": 61}],
    },
    {
        "id": 555,
        "title": "Sundown Shorts",
        "handle": "sundown-shorts",
        "product_type": "Shorts",
        "vendor": "Nimbus Outfitters",
        "status": "draft",
        "tags": "shorts, casual",
        "body_html": "<p>Casual summer shorts.</p>",
        "images": [],
        "faq": [],
        "metafields": {},
        "reviews": {"count": 0, "avg": 0, "source": ""},
        "variants": [{"id": 905, "sku": "SD-20", "price": "58.00", "inventory": 0}],
    },
]
