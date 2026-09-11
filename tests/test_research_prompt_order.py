"""§17.1024 — fresh sources must outrank the project's own prior output.

The operator asked the same question three times and got byte-similar generic
advice each time. Retrieval was not the problem by the third: the engine had
already fetched `reddit.com/r/Spectrum/.../how_to_set_sax1v1k_port_forwarding`
and the Askey manual. The PROMPT was.

    user content = {job_context}          <- 30,255 chars, FIRST
                   Question: {question}
                   {sources}              <- the fetched material, LAST

One variable changed on their live session, nothing else:

    job_context = 30,255 chars -> "go to 192.168.1.1 ... check the sticker"
    job_context = dropped      -> "Services -> Router -> Advanced Settings
                                   -> Port Forwarding & IP Reservations"

`job_context` carries `job_digest` — a digest of the job's own completed work,
which by then contained the three previous WRONG answers. Every wrong answer
was digested into the context that steered the next one. That loop is why the
text barely changed.
"""
import inspect

from app.config import Settings
from app.modules import assist_research_lib as lib

SRC = inspect.getsource(lib.research_one)


def test_the_question_and_sources_come_before_project_context():
    i_q = SRC.index('f"Question: {question}')
    i_src = SRC.index("_render_research_block(sources)")
    i_ctx = SRC.rindex('f"{ctx_block}"')
    assert i_q < i_src < i_ctx, (
        "project context must not precede the retrieved sources — leading with "
        "30 KB of the project's own prior output is what buried them"
    )


def test_project_context_is_bounded():
    assert "assist_job_context_max_chars" in SRC, "the context is unbounded again"
    assert "_ctx_raw[:_cap]" in SRC


def test_the_cap_has_a_floor_so_context_is_never_erased():
    assert "max(1000," in SRC, "a zero/negative cap would delete project context"


def test_project_context_is_framed_as_background_not_fact():
    """It is authoritative for OUR values and decisions, and not for facts
    about someone else's product — which is exactly what was asked."""
    assert "Project background" in SRC
    assert "NOT as a source of" in SRC


def test_the_trim_is_observable():
    assert "assist_research_ctx_trimmed" in SRC, "a silent trim cannot be triaged"


def test_the_cap_default_is_sane():
    cap = Settings().assist_job_context_max_chars
    assert 1000 <= cap <= 20000, cap
    assert cap < 30255, (
        "the live failure carried 30,255 chars; the cap must actually bind"
    )
