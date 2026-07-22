# task-02 — Shared Contracts (integration contract, schemas, DTO)

- **Пакет:** єдиний контракт адаптерів + Pydantic-схеми/DTO, проти яких кодують усі
- **Хвиля:** A (стартує одразу, головний розблоковувач паралельності)
- **Depends on:** — (може використати скелет task-01, але не блокується ним для написання типів)
- **Blocks:** task-04, task-05, task-06, task-09, task-10, task-11
- **PLAN.md:** §7 (єдиний integration contract), §6 (модель даних — DTO дзеркалять її), §10 (REST API), §3.2 (metric catalog поля)

## Мета

Зафіксувати **раз і назавжди** типи на межах модулів, щоб кожен агент кодував проти них незалежно. Це «швейцарський роз'єм» проєкту: адаптери провайдерів, worker, metrics-engine, API і фронт домовляються тут. Реалізацій немає — лише Protocol'и, Pydantic-моделі, enum'и та докстрінги.

> **Це найчутливіша до сумісності таска.** Будь-яка пізніша зміна тут — контрактна зміна, яку синхронізують усі споживачі. Проєктуй консервативно й повно.

## Обсяг

**Входить:**
- `app/integrations/contract.py` — `ProviderAdapter` Protocol + допоміжні типи виклику/результату.
- `app/integrations/registry.py` — реєстр адаптерів `Provider → ProviderAdapter` (порожній реєстр + API реєстрації; наповнюють провайдер-таски).
- `app/schemas/` — усі Pydantic v2 DTO: adapter-рівень, metric spec, API request/response.
- Enum'и: `Provider`, `PeriodKind`, `ComparisonMode`, `PeriodRole`, `Quality`, `BetterDirection`, `JobStatus`, `StageStatus`, `SourceMode`, `MetricUnit`.

**НЕ входить:** реалізації адаптерів (09–11), сам каталог метрик-записів (task-04 наповнює за типом `MetricSpec`), ORM-моделі (task-03 — але вони мають лишатися сумісними з DTO), роутери (task-06).

## Owned paths
```
app/integrations/contract.py
app/integrations/registry.py
app/schemas/__init__.py
app/schemas/enums.py
app/schemas/adapter.py       # ProgressEvent, CollectionContext, CollectionResult, PreflightResult
app/schemas/metrics.py       # MetricSpec, MetricValueDTO
app/schemas/api.py           # DashboardResponse, ClientDTO, JobDTO, ... (форми REST)
```

## Consumes (contracts)
Немає (це першоджерело).

## Provides — усі споживають звідси

### Enum'и (`app/schemas/enums.py`)
```python
class Provider(StrEnum):
    ahrefs = "ahrefs"; semrush = "semrush"; gsc = "gsc"; reporting_ninja = "reporting_ninja"

class PeriodKind(StrEnum): week = "week"; month = "month"
class ComparisonMode(StrEnum): closed = "closed"; to_date = "to_date"
class PeriodRole(StrEnum): current = "current"; previous = "previous"; snapshot = "snapshot"
class Quality(StrEnum):
    complete = "complete"; partial = "partial"; truncated = "truncated"
    stale = "stale"; unavailable = "unavailable"
class BetterDirection(StrEnum): up = "up"; down = "down"; neutral = "neutral"
class SourceMode(StrEnum): auto = "auto"; api = "api"; browser = "browser"; pdf = "pdf"
class JobStatus(StrEnum):
    queued="queued"; running="running"; succeeded="succeeded"; partial="partial"
    failed="failed"; cancel_requested="cancel_requested"; cancelled="cancelled"
class StageStatus(StrEnum):
    pending="pending"; running="running"; succeeded="succeeded"; failed="failed"; skipped="skipped"
class MetricUnit(StrEnum):
    count="count"; percent="percent"; score="score"; position="position"; currency="currency"; ratio="ratio"
```
Стадії кроку (стабільний порядок, PLAN §9): `Queued → Preflight → Authenticate → Fetch current → Fetch previous/history → Normalize → Validate → Persist → Complete`.

### Adapter contract (`app/integrations/contract.py` + `app/schemas/adapter.py`)
```python
class ProgressEvent(BaseModel):        # стабільні поля; message НЕ джерело логіки
    provider: Provider
    stage: str
    completed: int | None = None
    total: int | None = None
    percent: float | None = None       # 0..100
    level: Literal["info","warning","error"] = "info"
    message: str = ""                   # redacted, human-readable
    details: dict[str, Any] = {}

class DateRange(BaseModel):
    start: date
    end: date                            # inclusive; UI показує точні дати

class CollectionContext(BaseModel):
    client_id: str
    provider: Provider
    source_mode: SourceMode
    period_kind: PeriodKind
    comparison_mode: ComparisonMode
    current: DateRange
    previous: DateRange
    timezone: str                        # напр. "America/New_York"
    force_refresh: bool = False
    # non-secret зовнішні ідентифікатори (property/account/project id, database, report name)
    integration_config: dict[str, Any] = {}
    # шляхи до змонтованих сесій/профілів (НЕ секрети всередині БД)
    session_paths: dict[str, str] = {}

class MetricValueResult(BaseModel):
    metric_key: str
    period_role: PeriodRole
    period_start: date | None = None     # None для snapshot
    period_end: date | None = None
    value_numeric: Decimal | None = None # None ≠ 0
    value_text: str | None = None
    unit: MetricUnit
    quality: Quality
    is_derived: bool = False

class RawArtifactRef(BaseModel):
    kind: str                            # json|csv|pdf|screenshot|...
    path: str                            # relative, всередині artifacts root
    sha256: str
    mime: str | None = None
    size_bytes: int | None = None

class CollectionResult(BaseModel):
    provider: Provider
    quality: Quality                     # агрегатна якість збору
    observed_at: datetime                # UTC
    data_through: date | None = None     # GSC data lag тощо
    metrics: list[MetricValueResult]
    warnings: list[str] = []
    error_code: str | None = None        # безпечний код, без секретів
    provenance: dict[str, Any] = {}      # source_mode, source_version/AISeoTools commit
    artifacts: list[RawArtifactRef] = []
    units_spent: int | None = None
    units_estimated: int | None = None

class PreflightResult(BaseModel):
    provider: Provider
    ok: bool
    reason_code: str | None = None       # auth_required|no_project|no_units|no_profile|...
    message: str = ""
    units_estimated: int | None = None
    details: dict[str, Any] = {}

class ProviderAdapter(Protocol):
    provider: Provider
    def preflight(self, context: CollectionContext) -> PreflightResult: ...
    def collect(
        self,
        context: CollectionContext,
        emit: Callable[[ProgressEvent], None],
        is_cancel_requested: Callable[[], bool],
    ) -> CollectionResult: ...
```
**Інваріанти контракту:**
- `collect` **не кидає** на partial-даних: повертає `CollectionResult(quality=partial|truncated|unavailable, warnings=[...])`.
- `emit` — best-effort; адаптер не залежить від його результату.
- `is_cancel_requested()` перевіряється на безпечних межах stage; скасування кооперативне (не вбиває активний запис).
- Секрети ніколи не потрапляють у `ProgressEvent`, `warnings`, `error_code`, `provenance`.

### Registry (`app/integrations/registry.py`)
```python
def register_adapter(adapter: ProviderAdapter) -> None: ...
def get_adapter(provider: Provider) -> ProviderAdapter: ...   # raises if missing
def available_providers() -> list[Provider]: ...
```
Провайдер-таски (09–11) реєструють свій адаптер; worker (task-05) резолвить через `get_adapter`.

### MetricSpec (`app/schemas/metrics.py`) — тип запису каталогу; наповнює task-04
```python
class MetricSpec(BaseModel):
    key: str
    label: str                           # укр. підпис у UI
    provider: Provider
    unit: MetricUnit
    comparison_kind: Literal["range","snapshot"]
    better_direction: BetterDirection
    precision: int = 0
    allow_derived: bool = False
    freshness_sla_hours: int | None = None
    min_quality_to_show: Quality = Quality.partial
```

### API DTO (`app/schemas/api.py`) — форма REST-відповідей (PLAN §10)
Мінімум: `MetricCardValue` (current/previous/delta/quality/source/ranges), `ProviderSummaryDTO`, `ClientCardDTO`, `DashboardResponse`, `ClientDTO`, `IntegrationDTO`, `JobDTO`, `JobStepDTO`, `ProvidersStatusDTO`.
- Delta рахує backend (`delta_abs`, `delta_pct: Decimal | None`); при previous `0/null` → `delta_pct=None` + `delta_reason`. Фронт бізнес-семантику **не** рахує.

## Кроки реалізації
1. Enum'и.
2. Adapter-схеми + Protocol + інваріанти в докстрінгах.
3. Registry (порожній, потокобезпечний на рівні одного worker).
4. `MetricSpec`, `MetricValueDTO`.
5. API DTO дзеркально до §10 і §6 (узгодити назви полів із task-03 ORM і task-06).
6. `app/schemas/__init__.py` реекспортує публічні імена.

## Тести
- `tests/unit/schemas/` — валідація моделей, серіалізація Decimal, `None`-поля не колапсують у `0`, enum-значення стабільні (contract snapshot-тест на JSON-схему).
- Registry: register/get/duplicate/ missing.

## Definition of Done / Gate
- [ ] Усі типи імпортуються без циклів; `mypy` чистий.
- [ ] JSON-schema snapshot зафіксований (щоб зміни контракту були помітні в diff).
- [ ] Docstring кожного інваріанта присутній.
- [ ] Немає жодної реалізації провайдера/логіки — лише контракти.
- [ ] task-04/05/06/09/10/11 можуть імпортувати все потрібне звідси.

## Ризики
- Недовизначений контракт → пізніші болючі зміни в усіх споживачів. Краще перекрити поля наперед (usage/units, provenance, data_through вже враховані).
- Розбіжність назв полів DTO ↔ ORM (task-03) ↔ API (task-06): узгодь нейминг у цьому файлі як канон.
