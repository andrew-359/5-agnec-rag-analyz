"""Select the article passages that explain a figure experiment."""

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from ..core.domain import MULTI_HOP_TASK, Evidence
from ..run import IncompatibleArtifact
from ..strategies.text_pdfplumber import load_article_fragments, parse_text_fragment, validate_sources
from .selection import VERSION


SENSITIVITY_CAPTION = re.compile(r"\bhyperparameter (?:sensitivity )?analysis on (?:the )?([\w.-]+) dataset\b", re.I)
FIGURE_METHOD = re.compile(r"\b([\w.-]+) (?:demonstrates|achieves|maintains)\b[^.!?]{0,220}\bExact Match \(EM\)", re.I)
FIGURE_GENERATOR = re.compile(r"\busing (?:the )?([\w.-]+) model\b", re.I)


@dataclass(frozen=True)
class FigureExperiment:
    description: str
    method: str
    generator: str
    dataset: str
    task: str
    protocol: str
    context: Evidence
    setup: Evidence


def continuous_text(text: str) -> str:
    text = re.sub(r"(?<=\w)-[ \t]*\n[ \t]*(?=[a-z])", "", text)
    text = re.sub(r"(?<=\w)-[ \t]*\n[ \t]*(?=\d)", "-", text)
    return " ".join(text.split())


def unique_experiment(caption: dict, figure: str, fragments: tuple[Evidence, ...]) -> FigureExperiment | None:
    dataset = SENSITIVITY_CAPTION.search(caption["text"])
    if not dataset:
        return None
    dataset = dataset[1]
    mention = re.compile(rf"\b(?:Figure|Fig\.)\s*{re.escape(figure)}(?!\w|[.-]\w)", re.I)
    dataset_reference = re.compile(rf"\b{re.escape(dataset)}\b", re.I)
    contexts = []
    for fragment in fragments:
        if abs(fragment.page - caption["page"]) > 1:
            continue
        text = continuous_text(fragment.text)
        if not (mention.search(text) and dataset_reference.search(text) and re.search(r"\bhyperparameter analysis\b", text, re.I)):
            continue
        methods = set(FIGURE_METHOD.findall(text))
        generators = set(FIGURE_GENERATOR.findall(text))
        if len(methods) == len(generators) == 1:
            contexts.append((fragment, methods.pop(), generators.pop()))
    if len(contexts) != 1:
        return None
    context, method, generator = contexts[0]
    setup = []
    for fragment in fragments:
        text = continuous_text(fragment.text)
        if fragment.page > context.page or not (dataset_reference.search(text) and MULTI_HOP_TASK.search(text)):
            continue
        if not re.search(rf"\b(?:evaluate|evaluated)\s+{re.escape(method)}\s+on\b", text, re.I):
            continue
        protocol = re.search(r"\bFor evaluation, [^.]+\bExact Match \(EM\)[^.]+\.", text, re.I)
        if protocol:
            task = "multi-hop question answering"
            if re.search(r"\bopen-domain multi-hop\b", text, re.I):
                task = f"open-domain {task}"
            setup.append((fragment, task, protocol[0]))
    if len(setup) == 1:
        evaluated, task, protocol = setup[0]
        return FigureExperiment(caption["text"], method, generator, dataset, task, protocol, context, evaluated)
    return None


@dataclass(frozen=True)
class PreparedFigureContext:
    sha256: str
    candidate: dict
    experiment: FigureExperiment | None

    def artifact(self) -> dict:
        candidate = self.candidate
        caption = Evidence(candidate["id"], candidate["page"], candidate["text"], candidate["box"], "figure")
        basis = [{"kind": "caption", "source": asdict(caption)}]
        experiment = self.experiment
        if experiment is not None:
            basis.extend([
                {"kind": "results_reference", "source": asdict(experiment.context)},
                {"kind": "setup_section", "source": asdict(experiment.setup)},
            ])
        return {
            "schema_version": VERSION,
            "sha256": self.sha256,
            "target": {"candidate": candidate["id"], "page": candidate["page"],
                       "label": candidate["label"], "box": candidate["box"]},
            "status": "supported" if experiment else "caption_only",
            "reason": ("Подпись, ссылка и настройки связывают рисунок с экспериментом." if experiment else
                       "Доступна подпись рисунка. Сведения об эксперименте требуют проверки."),
            "basis": basis,
            "experiment": asdict(experiment) if experiment else None,
        }


def prepare_figure_context(
    shared_text: Path, sha256: str, pages: int, candidate: dict,
) -> PreparedFigureContext:
    try:
        article = load_article_fragments(shared_text, sha256)
        if article.pages != tuple(range(1, pages + 1)):
            raise ValueError("Сохранённый общий текст должен охватывать все страницы PDF.")
    except (OSError, KeyError, TypeError, ValueError) as error:
        raise IncompatibleArtifact(f"Несовместимый артефакт общего текста. {error}") from error
    return PreparedFigureContext(sha256, candidate, unique_experiment(candidate, candidate["label"], article.fragments))


def load_figure_context(path: Path, sha256: str, pages: int, candidate: dict) -> PreparedFigureContext:
    try:
        data = json.loads(path.read_bytes())
        if type(data["schema_version"]) is not int or data["schema_version"] != VERSION:
            raise ValueError("Неизвестная версия формата подборки.")
        if data["sha256"] != sha256:
            raise ValueError("SHA-256 подборки не совпадает с версией PDF прогона.")
        experiment = None
        if (saved := data["experiment"]) is not None:
            context, setup = (parse_text_fragment(saved[field]) for field in ("context", "setup"))
            strings = {field: saved[field] for field in ("description", "method", "generator", "dataset", "task", "protocol")}
            if not all(isinstance(value, str) and value.strip() for value in strings.values()):
                raise ValueError("Некорректное описание эксперимента.")
            validate_sources(sha256, (context, setup))
            if context.page > pages or setup.page > pages:
                raise ValueError("Страница фрагмента находится за пределами PDF.")
            experiment = FigureExperiment(**strings, context=context, setup=setup)
        prepared = PreparedFigureContext(sha256, candidate, experiment)
        if json.loads(json.dumps(prepared.artifact())) != data:
            raise ValueError("Подборка относится к другому рисунку или содержит несовместимые источники.")
        return prepared
    except (OSError, KeyError, TypeError, ValueError) as error:
        raise IncompatibleArtifact(f"Несовместимая подборка контекста. {error}") from error
