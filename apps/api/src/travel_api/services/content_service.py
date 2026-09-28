"""Storing and reading back a traveller's own content.

Sits between the extractor (which is pure) and the database (which is not), and owns the
two things neither of them should: identity, and owner scoping.

Owner scoping is the reason this is a service rather than a repository call in the route.
A saved 小红书 note is a person's own material — it may name where they ate and when they
were away — so every read filters on the owner, and ``None`` (the unauthenticated
single-user mode) is kept distinct from ``""`` (a real anonymous owner). Collapsing those
two would let one anonymous user read another's saved notes.
"""

from __future__ import annotations

from uuid import UUID, uuid4

from travel_api.domain.ingestion import Ingestion, IngestionRequest
from travel_api.services.ingestion import ContentExtractionEngine


class ContentIngestionService:
    """Persistence for user-supplied content, scoped per owner."""

    def __init__(
        self,
        uow_factory,
        engine: ContentExtractionEngine | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self.engine = engine or ContentExtractionEngine()

    def ingest(self, request: IngestionRequest, *, owner_id: str | None = None) -> Ingestion:
        """Extract and store one supplied note.

        Identity is generated here rather than by the database so the extraction can be
        named in a log line before the row exists, which is what makes a failed insert
        traceable to the input that caused it.
        """
        ingestion = self.engine.extract(request, ingestion_id=str(uuid4()))
        ingestion.owner_id = owner_id
        with self._uow_factory() as uow:
            uow.ingestions.create(ingestion)
        return ingestion

    def get(self, ingestion_id: UUID, *, owner_id: str | None = None) -> Ingestion | None:
        with self._uow_factory() as uow:
            return uow.ingestions.get(ingestion_id, owner_id=owner_id)

    def list(self, *, owner_id: str | None = None, limit: int = 50) -> list[Ingestion]:
        with self._uow_factory() as uow:
            return uow.ingestions.list_for_owner(owner_id, limit=limit)

    def delete(self, ingestion_id: UUID, *, owner_id: str | None = None) -> bool:
        with self._uow_factory() as uow:
            return uow.ingestions.delete(ingestion_id, owner_id=owner_id)

    def source_references(self, *, owner_id: str | None = None) -> list:
        """A reference per stored note, for inclusion in a plan's citations.

        Every plan must be able to say where its content came from, and a note the
        traveller supplied is one of those sources. Each reference carries whether the
        page stated a publication date, so an undated note is reported as having an
        unknown data age rather than being presented as current.
        """
        return [item.source_reference() for item in self.list(owner_id=owner_id, limit=200)]

    def bookmarklet(self, request) -> dict:
        """The bookmarklet source and the instructions that make it usable.

        The bookmarklet runs in the traveller's own browser, on the page they are
        already logged into. That is the whole point: this service never holds a session
        for 小红书 or 抖音, never scrapes, and never makes a request to those hosts.
        What it receives is text the traveller could have copied by hand.
        """
        # Built from the request's own base URL, so the snippet posts back to the same
        # origin the console is served from. A deployment behind a proxy therefore gets
        # a bookmarklet pointing at the address the traveller is actually using.
        base = str(request.base_url).rstrip("/")
        return {
            "href": _BOOKMARKLET_TEMPLATE.replace("__BASE__", base),
            "base_url": base,
            "steps": [
                "把下面的按钮拖到浏览器书签栏（或新建一个书签，把地址粘贴进去）。",
                "在已经登录的页面（小红书 / 抖音 / 大众点评 / 携程等）点击这个书签。",
                "脚本会在你的浏览器里读取当前页面正文与发布时间，然后发回 Traver。",
                "回到 Traver 的「内容摄取」页面，确认提取结果。",
            ],
            "notes": [
                "脚本在你自己已登录的浏览器里运行，Traver 不会、也无法代你登录这些平台。",
                "它只发送当前页面的标题、正文文本、作者、发布时间与网址。",
                "如果页面没有写发布时间，Traver 会明确记录「发布时间未知」，不会当作最新的内容。",
            ],
        }


#: The bookmarklet body, assembled from readable parts so it can be audited line by line
#: and still be a single-line snippet in the browser.
#:
#: A traveller is being asked to run this in their own logged-in browser, so it has to be
#: short enough to read: it touches ``document.body.innerText`` and a couple of metadata
#: tags, and never cookies, tokens or storage. ``innerText`` rather than ``innerHTML``
#: because the extractor reads prose — sending markup would add hundreds of kilobytes of
#: navigation and styling to convey the same sentences.
_BOOKMARKLET_PARTS = (
    "javascript:(function(){",
    # One helper for the three optional metadata reads, so an absent tag yields "" rather
    # than throwing and aborting the whole snippet.
    "var pick=function(s){var e=document.querySelector(s);"
    "return e?(e.content||e.innerText||e.getAttribute('datetime')||''):''};",
    "var d=pick('meta[property=\"article:published_time\"]')||pick('time');",
    "var payload={text:(document.body?document.body.innerText:''),url:location.href,"
    "title:document.title||'',author:pick('meta[name=author]')||null,"
    "published_at:d||null};",
    "fetch('__BASE__/api/v1/ingestions',{method:'POST',"
    "headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)})",
    ".then(function(r){return r.json()})",
    ".then(function(j){alert('Traver 已读取本页：识别到 '+(j.facts?j.facts.length:0)+"
    "' 条可结构化内容。')})",
    ".catch(function(e){alert('发送失败：'+e)});",
    "})()",
)

_BOOKMARKLET_TEMPLATE = "".join(_BOOKMARKLET_PARTS)
