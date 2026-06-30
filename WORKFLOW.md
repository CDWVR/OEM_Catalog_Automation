# Product Lifecycle Query Engine v2 — Workflow Diagrams

Reference for how commands, data stores, and the 5-layer query engine fit together.

---

## 1. High-level system

```mermaid
flowchart TB
    subgraph CLI["CLI (main.py)"]
        Q[query / batch / interactive]
        C[crawl]
        D[discover]
        S[stats]
        R[reset-db]
    end

    subgraph Prep["Preparation"]
        SD[seed_data.py hubs]
        OD[oem_discovery.py]
    end

    subgraph Store["SQLite lifecycle_v2.db"]
        HUB[oem_hub_urls]
        DOM[oem_dynamic_domains]
        DISC[discovered_urls + snippets]
        PROD[products cache]
        SC[search_cache]
    end

    subgraph Crawl["crawler.py"]
        BFS[BFS domain-locked crawl]
        SCR[scraper.py]
    end

    subgraph Engine["engine.py — 5 layers"]
        L0[L0 cache]
        L1[L1 endoflife.date]
        L2[L2 crawl DB]
        L3[L3 search + Gemini]
        L4[L4 not found]
    end

    SD --> HUB
    D --> OD
    OD --> HUB
    OD --> DOM
    C --> BFS
    BFS --> SCR
    SCR --> DISC

    Q --> Engine
    L2 --> DISC
    L3 --> SC
    L3 --> SCR
    L0 --> PROD
    L1 --> PROD
    L2 --> PROD
    L3 --> PROD
```

---

## 2. Recommended operational workflow

```mermaid
flowchart LR
    A[Setup .env<br/>GEMINI + EXA] --> B{OEM in seed_data?}
    B -->|No| C[discover -o OEM<br/>optional --crawl]
    B -->|Yes| D[crawl --oem OEM]
    C --> D
    D --> E[query -p product -o OEM]
    E --> F{found?}
    F -->|Yes| G[Result in products cache]
    F -->|No| H[Check FAIL REPORT<br/>crawl deeper / fix OEM]
    H --> D
    G --> I[batch Excel optional]
```

---

## 3. Query pipeline — when does it end?

```mermaid
flowchart TD
    START([query -p product -o oem]) --> PRE[ensure_oem_ready<br/>auto-discover if needed]
    PRE --> PREP[prepare_query<br/>version + aliases]

    PREP --> L0{Layer 0<br/>products cache?}
    L0 -->|hit, TTL OK| END_OK[END ✓ found=True]
    L0 -->|miss or --refresh| L1{Layer 1<br/>endoflife.date}

    L1 -->|match| END_OK
    L1 -->|miss| L2{Layer 2<br/>discovered_urls}

    L2 --> L2A[Rank URLs, load snippets/scrape<br/>up to 5 Gemini extractions]
    L2A --> L2B{accept_as_found?<br/>≥0.55 or partial ≥0.42}
    L2B -->|yes| END_OK
    L2B -->|no| L3[Layer 3]

    L3 --> L3A[search_cache → crawl DB → Exa → DDGS<br/>up to 20 URLs]
    L3A --> L3B[Scrape 3 workers<br/>score pages, top 5 → Gemini]
    L3B --> L3C{accept_as_found?}
    L3C -->|yes, early or after pipeline| END_OK
    L3C -->|no| L4[FAIL REPORT]
    L4 --> END_FAIL[END ✗ found=False + reason]

    END_OK --> SAVE[(save products)]
```

### Query stop rules (summary)

| Outcome | Condition |
|--------|-----------|
| **Success (full)** | `confidence ≥ 0.55` |
| **Success (partial)** | `confidence ≥ 0.42` (default) + at least one lifecycle field |
| **Failure** | All layers exhausted without acceptable extraction |
| **Instant** | Layer 0 cache hit (no `--refresh`) |

---

## 4. Layer 3 detail (web search path)

```mermaid
flowchart TD
    A[search_product_lifecycle] --> B{search_cache fresh?}
    B -->|yes| Z[Return cached URLs]
    B -->|no| C[rank_discovered_urls_for_product]
    C --> D[Exa lifecycle search<br/>+ alias variants]
    D --> E{Enough URLs?}
    E -->|no| F[DDGS fallback]
    E -->|yes| G[Save search_cache]
    F --> G
    G --> H[Scrape URLs concurrent]
    H --> I[_score_page<br/>text + URL lifecycle score]
    I --> J[Top 5 pages → extract_from_content Gemini]
    J --> K[calculate_confidence<br/>lifecycle_status, fields, domain]
    K --> L{accept_as_found?}
    L -->|yes| M[Return QueryResult]
    L -->|no| N[Next page or Layer 4]
```

---

## 5. Crawl workflow (BFS)

```mermaid
flowchart TD
    START([crawl --oem X]) --> SEED[get_crawl_seed_urls<br/>hubs + promoted URLs]
    SEED --> Q[Queue depth=0 seeds]
    Q --> LOOP{queue not empty AND<br/>scraped < 200?}
    LOOP -->|no| DONE([Crawl complete])
    LOOP -->|yes| POP[Dequeue URL at depth d]
    POP --> DCHK{d > max_depth?}
    DCHK -->|yes| LOOP
    DCHK -->|no| SCR[scrape_url need_links=True]
    SCR --> SAVE[discovered_urls + gzip snippet]
    SAVE --> LINKS[Enqueue child links<br/>same OEM domain only]
    LINKS --> PROMO{depth ≤ 2 and<br/>lifecycle type?}
    PROMO -->|yes| HUB[promote_to_hub]
    PROMO -->|no| LOOP
    HUB --> LOOP
```

### Crawl limits

| Setting | Default | How to change |
|--------|---------|----------------|
| **Max depth** | `4` | `--depth N` or `CRAWL_MAX_DEPTH` in `.env` |
| **Max pages scraped / session** | `200` | `MAX_URLS_PER_OEM` in `crawler.py` |
| **Depth meaning** | Link hops from hub | 0 = seed, 1 = child, … |

---

## 6. OEM discovery workflow

```mermaid
flowchart TD
    START([discover -o OEM]) --> SEARCH[Exa + DDGS<br/>lifecycle queries]
    SEARCH --> SCORE[Score URL paths]
    SCORE --> TOP[Top ~8 URLs → oem_hub_urls]
    SCORE --> DOM[Infer domains → oem_dynamic_domains]
    TOP --> CRAWL{--crawl?}
    DOM --> CRAWL
    CRAWL -->|yes| BFS[crawl_oem]
    CRAWL -->|no| END([Done])
    BFS --> END
```

Auto-discovery on **query** uses the same hub/domain registration when `OEM_AUTO_DISCOVER=true` and hubs/domains are missing (no BFS unless you run `discover --crawl` separately).

---

## 7. Data flow by command

```mermaid
flowchart LR
    subgraph query_cmd["main.py query"]
        Q1[product + OEM] --> E1[engine.query]
        E1 --> P1[(products)]
    end

    subgraph crawl_cmd["main.py crawl"]
        C1[OEM] --> E2[crawl_oem]
        E2 --> P2[(discovered_urls)]
        E2 --> P3[(oem_hub_urls promoted)]
    end

    subgraph discover_cmd["main.py discover"]
        D1[OEM] --> E3[discover_and_register_oem]
        E3 --> P4[(oem_hub_urls)]
        E3 --> P5[(oem_dynamic_domains)]
    end

    subgraph reset_cmd["main.py reset-db --yes"]
        R1[clear tables] --> P6[(empty or re-seeded)]
    end
```

---

## 8. CLI quick reference

```text
Interactive     python main.py
Query           python main.py query -p "Product" -o "OEM" [--refresh]
Batch           python main.py batch -i in.xlsx -o out.xlsx
Crawl           python main.py crawl --oem "OEM" [--depth N]
                python main.py crawl --all [--depth N]
Discover        python main.py discover -o "OEM" [-p product] [--crawl] [--depth N]
Stats           python main.py stats
Reset           python main.py reset-db --yes [--oem OEM] [--delete-file]
```

Windows: prefix with `.\venv\Scripts\python.exe` if `python` is not on PATH.

---

## 9. Typical end-to-end (new OEM)

```mermaid
sequenceDiagram
    participant U as User
    participant M as main.py
    participant OD as oem_discovery
    participant CR as crawler
    participant DB as SQLite
    participant EN as engine
    participant EX as Gemini / Exa

    U->>M: discover -o Adobe --crawl
    M->>OD: search + register hubs/domains
    OD->>DB: oem_hub_urls, oem_dynamic_domains
    M->>CR: crawl_oem(Adobe)
    CR->>DB: discovered_urls + snippets

    U->>M: query -p "Photoshop 2025" -o Adobe
    M->>EN: query()
    EN->>DB: Layer 0 miss
    EN->>EN: Layer 1 endoflife.date miss
    EN->>DB: Layer 2 rank discovered_urls
    alt Layer 2 hit
        EN->>EX: Gemini extract
        EN->>DB: save products
        EN-->>U: found=True
    else Layer 2 miss
        EN->>EX: Layer 3 Exa + scrape + Gemini
        EN-->>U: found=True or FAIL REPORT
    end
```
