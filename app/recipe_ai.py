"""Claude-based video understanding.

Two tiers:
  * Haiku 5.5 reads *every* sampled frame in parallel batches (cheap OCR + "what's on screen").
  * Opus 5.5 (adaptive thinking) reconciles caption, comments, timestamped transcript, the frame
    timeline, and the most informative frames themselves into one structured recipe.
"""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any, List, Literal, Optional, Sequence

from pydantic import BaseModel, Field

from app.constants import (
    EXTRACT_MAX_TOKENS,
    FRAME_BATCH_SIZE,
    MAX_EXTRACTION_FRAMES,
    VISION_MAX_TOKENS,
)

if TYPE_CHECKING:
    from anthropic import Anthropic

    from app.media_pipeline import Frame
    from app.settings import Settings


# ---------------------------------------------------------------------------
# Schemas (all fields required: structured outputs caps optional/nullable params)
# ---------------------------------------------------------------------------

IngredientSource = Literal["caption", "speech", "on_screen_text", "comment", "visual_estimate", "inferred"]


class Ingredient(BaseModel):
    amount: str = Field(description='Quantity as stated, e.g. "1", "1/2", "1 1/4", "2-3". Empty if none.')
    unit: str = Field(description="Unit as stated (cup, tbsp, g, clove, ...). Empty string if countable/none.")
    item: str = Field(description="Food name. For section headers, the header text.")
    note: str = Field(description="Prep/state notes (diced, room temp, divided). Empty if none.")
    is_section_header: bool
    no_amount: bool = Field(description="True for to-taste / unmeasured items.")
    source: IngredientSource = Field(description="Where the amount came from (strongest evidence).")


class Step(BaseModel):
    name: str
    instruction: str
    time_minutes: int = Field(description="0 if unknown.")
    ingredients: List[Ingredient]


class RecipeExtraction(BaseModel):
    is_recipe: bool = Field(description="False if the video does not actually contain a recipe.")
    title: str
    description: str
    servings: int = Field(description="0 if unknown.")
    servings_text: str
    prep_time_minutes: int = Field(description="0 if unknown.")
    cook_time_minutes: int = Field(description="0 if unknown.")
    passive_time_minutes: int = Field(description="0 if unknown.")
    cuisine: str
    keywords: List[str]
    equipment: List[str]
    steps: List[Step]
    tips: List[str] = Field(description="Creator tips, substitutions, storage notes.")
    uncertainties: List[str] = Field(
        description="Short notes on conflicts, guesses, or missing info (e.g. 'full recipe is on creator's site')."
    )


class FrameNote(BaseModel):
    frame_index: int
    on_screen_text: str = Field(description="All legible overlay/caption/label text, verbatim. Empty if none.")
    visible_ingredients: List[str] = Field(description="Ingredients/products visible, with any readable amounts.")
    action: str = Field(description="What the cook is doing, in a few words.")
    has_recipe_info: bool = Field(description="True if this frame shows amounts, ingredients, temps or times.")


class FrameNotes(BaseModel):
    frames: List[FrameNote]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _image_block(path: str) -> dict:
    with open(path, "rb") as f:
        data = base64.standard_b64encode(f.read()).decode("ascii")
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}}


def _log_usage(label: str, response: Any) -> None:
    u = getattr(response, "usage", None)
    if u is not None:
        print(f"=== {label} usage: in={getattr(u, 'input_tokens', '?')} out={getattr(u, 'output_tokens', '?')} ===")


def _parsed(response: Any, model_cls: type) -> Any:
    parsed = getattr(response, "parsed_output", None)
    if parsed is not None:
        return parsed
    stop = getattr(response, "stop_reason", None)
    if stop == "refusal":
        raise RuntimeError("Claude declined to process this content.")
    if stop == "max_tokens":
        raise RuntimeError("Claude ran out of output tokens before finishing the recipe.")
    text = next((b.text for b in getattr(response, "content", []) if getattr(b, "type", "") == "text"), "")
    return model_cls.model_validate_json(text or "{}")


# ---------------------------------------------------------------------------
# Tier 1: frame OCR with Haiku
# ---------------------------------------------------------------------------

FRAME_SYSTEM = (
    "You read frames from a short cooking video. For each frame, transcribe every legible piece of "
    "on-screen text exactly (overlays, captions, measuring-cup markings, package labels, oven displays), "
    "list ingredients you can see, and describe the action briefly. Never guess unreadable text."
)


def describe_frames(client: "Anthropic", settings: "Settings", frames: Sequence["Frame"],
                    max_workers: int = 4) -> List[FrameNote]:
    if not frames:
        return []
    batches = [list(range(i, min(i + FRAME_BATCH_SIZE, len(frames))))
               for i in range(0, len(frames), FRAME_BATCH_SIZE)]

    def run(idx: List[int]) -> List[FrameNote]:
        content: List[dict] = []
        for i in idx:
            content.append({"type": "text", "text": f"Frame {i} (t={frames[i].t:.1f}s):"})
            content.append(_image_block(frames[i].path))
        content.append({"type": "text", "text": f"Return one entry per frame for frames {idx[0]}-{idx[-1]}."})
        try:
            res = client.messages.parse(
                model=settings.vision_model,
                max_tokens=VISION_MAX_TOKENS,
                system=FRAME_SYSTEM,
                messages=[{"role": "user", "content": content}],
                output_format=FrameNotes,
            )
            _log_usage(f"FRAMES {idx[0]}-{idx[-1]}", res)
            notes = _parsed(res, FrameNotes).frames
        except Exception as e:  # one bad batch shouldn't sink the import
            print(f"Frame batch {idx[0]}-{idx[-1]} failed: {e!r}")
            return []
        valid = set(idx)
        return [n for n in notes if n.frame_index in valid]

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        results = list(pool.map(run, batches))
    out = [n for batch in results for n in batch]
    out.sort(key=lambda n: n.frame_index)
    return out


def select_key_frames(frames: Sequence["Frame"], notes: Sequence[FrameNote],
                      limit: int = MAX_EXTRACTION_FRAMES) -> List[int]:
    """Frames with recipe info first (most text first), topped up with evenly spaced frames; chronological."""
    n = len(frames)
    if n <= limit:
        return list(range(n))
    informative = sorted(
        (x for x in notes if x.has_recipe_info and 0 <= x.frame_index < n),
        key=lambda x: len(x.on_screen_text) + 10 * len(x.visible_ingredients),
        reverse=True,
    )
    chosen = {x.frame_index for x in informative[:limit]}
    if len(chosen) < limit:
        step = n / (limit - len(chosen))
        k = 0.0
        while len(chosen) < limit and k < n:
            chosen.add(int(k))
            k += step
        i = 0
        while len(chosen) < limit and i < n:
            chosen.add(i)
            i += 1
    return sorted(chosen)


def format_frame_timeline(frames: Sequence["Frame"], notes: Sequence[FrameNote]) -> str:
    lines = []
    for x in notes:
        if not (0 <= x.frame_index < len(frames)):
            continue
        parts = [f"[{frames[x.frame_index].t:6.1f}s] {x.action}".rstrip()]
        if x.on_screen_text:
            parts.append(f'text: "{x.on_screen_text}"')
        if x.visible_ingredients:
            parts.append("visible: " + ", ".join(x.visible_ingredients))
        lines.append(" | ".join(parts))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tier 2: recipe extraction with Opus
# ---------------------------------------------------------------------------

EXTRACT_SYSTEM = """\
You turn short-form cooking videos (TikTok, Instagram Reels) into complete, accurate recipes for
Tandoor Recipes. You are given every signal we could pull from the video: the creator's caption,
the creator's own comments, popular viewer comments, a timestamped speech transcript, a timeline of
on-screen text and visible ingredients for every sampled frame, and the most informative frames
themselves.

How to reconcile evidence:
- Social recipes scatter information. Amounts are often ONLY in on-screen text or a creator comment;
  techniques are often ONLY spoken; some ingredients are only ever shown. Merge all of it.
- Precedence for amounts: on-screen text and caption > creator comments > speech > viewer comments.
  When sources disagree, use the more specific one and add a short note to `uncertainties`.
- Treat speech-to-text errors sensibly (e.g. "a quarter cup of mayo" heard as "a court cup"), using
  frames to confirm.
- If an ingredient is clearly used but no amount is given anywhere, you may estimate from the frames
  (e.g. a visibly full 1-cup measure) and set source="visual_estimate"; otherwise set no_amount=true.
  Never invent precise amounts without visual evidence.
- Keep units as the creator stated them; do not convert.
- Include every ingredient that appears, including garnishes, oil for the pan and seasoning to taste.
- Put each ingredient on the step where it is first used. Use section headers for components
  (e.g. "For the sauce") when the recipe has distinct parts.
- Steps must be complete, in order, and specific: include heat levels, temperatures (with units as
  stated), visual doneness cues, and times. Don't merge unrelated actions.
- Times: 0 when unknown; don't invent minutes.
- Capture creator tips, substitutions and storage advice in `tips`.
- If the full recipe is said to be elsewhere ("recipe in bio", a blog link), extract everything that IS
  in the video and say so in `uncertainties`.
- If the video is not a recipe at all, set is_recipe=false and leave lists empty.
- Title: a clean dish name (no emojis, hashtags or creator handles). Description: 1-3 plain sentences.
- Keywords: up to 8 short tags (dish type, main protein, diet, method). No hashtag symbols.
"""


def build_extraction_text(
    *,
    url: str,
    platform: str,
    title_hint: str,
    uploader: str,
    caption: str,
    tags: Sequence[str],
    comments: Sequence[dict],
    transcript: str,
    frame_timeline: str,
    key_frame_times: Sequence[float],
) -> str:
    creator = [c for c in comments if c.get("is_creator")]
    viewers = [c for c in comments if not c.get("is_creator")]

    def fmt(cs: Sequence[dict]) -> str:
        return "\n".join(f"- ({c.get('likes', 0)} likes) {c['text']}" for c in cs) or "(none)"

    frames_line = ", ".join(f"{t:.1f}s" for t in key_frame_times) or "(no frames)"
    return f"""<source>
url: {url}
platform: {platform}
creator: {uploader or "(unknown)"}
title hint: {title_hint or "(none)"}
hashtags: {", ".join(tags) or "(none)"}
</source>

<caption>
{caption or "(empty)"}
</caption>

<creator_comments>
{fmt(creator)}
</creator_comments>

<viewer_comments>
{fmt(viewers)}
</viewer_comments>

<transcript>
{transcript or "(no speech detected — the video may be music-only; rely on text and frames)"}
</transcript>

<frame_timeline>
{frame_timeline or "(not available)"}
</frame_timeline>

The attached images are frames at: {frames_line}

Extract the complete recipe."""


def extract_recipe(
    client: "Anthropic",
    settings: "Settings",
    *,
    frames: Sequence["Frame"],
    key_frames: Sequence[int],
    text: str,
) -> RecipeExtraction:
    content: List[dict] = []
    for i in key_frames:  # images before text works best
        content.append(_image_block(frames[i].path))
    content.append({"type": "text", "text": text})

    res = client.messages.parse(
        model=settings.extract_model,
        max_tokens=EXTRACT_MAX_TOKENS,
        system=EXTRACT_SYSTEM,
        thinking={"type": "adaptive"},
        output_config={"effort": settings.extract_effort},
        messages=[{"role": "user", "content": content}],
        output_format=RecipeExtraction,
    )
    _log_usage("EXTRACT", res)
    return _parsed(res, RecipeExtraction)


def make_client(settings: "Settings", factory: Optional[Any] = None) -> "Anthropic":
    if factory is None:
        from anthropic import Anthropic as factory
    # Generous timeout: Opus with thinking on ~24 images can take a minute.
    return factory(api_key=settings.anthropic_api_key, timeout=300.0, max_retries=3)
