"""Tests for Phase 1.10 Generative UI — widget schema + composer.

Coverage:
  - Schema validation (good specs, bad specs, safe fallback)
  - Deterministic composer: system_info → metrics; search results → cards;
    flat dict → key_value; code block → code widget
  - LLM compose path: mock litellm so no network; validates fallback on bad JSON
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from friday.gui.widgets import (
    BarChartWidget,
    CardsWidget,
    CodeWidget,
    KeyValueWidget,
    Layout,
    MarkdownWidget,
    MetricWidget,
    TableWidget,
    _detect_code,
    _looks_like_key_value,
    _looks_like_search_results,
    _looks_like_system_info,
    compose_layout,
)

# ── Schema tests ──────────────────────────────────────────────────────────────


class TestLayoutFallback:
    def test_fallback_creates_markdown_widget(self):
        layout = Layout.fallback("hello world")
        assert len(layout.widgets) == 1
        assert isinstance(layout.widgets[0], MarkdownWidget)
        assert layout.widgets[0].text == "hello world"

    def test_fallback_empty_string_gets_placeholder(self):
        layout = Layout.fallback("")
        assert layout.widgets[0].text == "(no content)"

    def test_from_raw_unknown_type_skipped(self):
        raw = [{"type": "unicorn", "text": "nope"}, {"type": "markdown", "text": "yes"}]
        layout = Layout.from_raw(raw)
        assert len(layout.widgets) == 1
        assert isinstance(layout.widgets[0], MarkdownWidget)

    def test_from_raw_all_bad_gives_fallback(self):
        raw = [{"type": "invalid_widget_type"}]
        layout = Layout.from_raw(raw)
        # Layout.from_raw returns fallback("") on empty widgets
        assert len(layout.widgets) == 1
        assert isinstance(layout.widgets[0], MarkdownWidget)

    def test_from_raw_empty_list_gives_fallback(self):
        layout = Layout.from_raw([])
        assert isinstance(layout.widgets[0], MarkdownWidget)


class TestWidgetSchemas:
    def test_markdown_widget(self):
        w = MarkdownWidget(text="hello")
        assert w.type == "markdown"
        assert w.text == "hello"

    def test_metric_widget(self):
        w = MetricWidget(label="CPU", value="42", unit="%")
        assert w.type == "metric"
        assert w.label == "CPU"

    def test_key_value_coercion(self):
        w = KeyValueWidget(pairs=[{"label": "foo", "value": "bar"}, {"label": "x"}])
        assert len(w.pairs) == 2
        assert w.pairs[0]["label"] == "foo"

    def test_bar_chart_aligns_lengths(self):
        w = BarChartWidget(labels=["a", "b", "c"], values=[1.0, 2.0])
        assert len(w.labels) == 2
        assert len(w.values) == 2

    def test_table_row_coercion(self):
        w = TableWidget(columns=["A", "B"], rows=[[1, 2], ["x", "y"]])
        assert w.rows[0] == ["1", "2"]

    def test_cards_item_coercion(self):
        w = CardsWidget(items=[{"title": "T", "url": "http://x.com"}])
        assert len(w.items) == 1
        assert w.items[0].title == "T"

    def test_code_widget(self):
        w = CodeWidget(language="bash", text="echo hello")
        assert w.type == "code"
        assert w.language == "bash"

    def test_layout_to_json_round_trips(self):
        layout = Layout(widgets=[
            MetricWidget(label="CPU", value="55", unit="%"),
            MarkdownWidget(text="all good"),
        ])
        data = layout.to_json()
        assert len(data) == 2
        assert data[0]["type"] == "metric"
        assert data[1]["type"] == "markdown"


# ── Shape detectors ───────────────────────────────────────────────────────────


class TestShapeDetectors:
    def test_system_info_detected(self):
        assert _looks_like_system_info({"cpu_pct": 10, "ram_pct": 50})

    def test_system_info_not_detected_for_other_dicts(self):
        assert not _looks_like_system_info({"name": "foo", "age": 30})

    def test_system_info_rejects_list(self):
        assert not _looks_like_system_info([{"cpu_pct": 10}])  # type: ignore[arg-type]

    def test_search_results_detected(self):
        results = [{"title": "Foo", "url": "http://foo.com", "content": "bar"}]
        assert _looks_like_search_results(results)

    def test_search_results_with_link_key(self):
        results = [{"title": "Foo", "link": "http://foo.com"}]
        assert _looks_like_search_results(results)

    def test_search_results_not_detected_for_plain_list(self):
        assert not _looks_like_search_results(["hello", "world"])

    def test_key_value_detected(self):
        assert _looks_like_key_value({"a": 1, "b": "two", "c": True})

    def test_key_value_not_for_nested_dict(self):
        assert not _looks_like_key_value({"a": {"nested": "value"}})

    def test_detect_code_fenced_block(self):
        text = "```python\nprint('hello')\n```"
        is_code, lang = _detect_code(text)
        assert is_code is True
        assert lang == "python"

    def test_detect_code_no_fence(self):
        is_code, _ = _detect_code("Just some plain text here.")
        assert is_code is False

    def test_detect_code_by_syntax(self):
        is_code, lang = _detect_code("def foo(x):\n    return x + 1")
        assert is_code is True
        assert lang == "python"


# ── Deterministic composer ────────────────────────────────────────────────────


class TestDeterministicComposer:
    def test_system_info_produces_metrics(self):
        data = {"cpu_pct": 45.2, "ram_pct": 62.1, "disk_pct": 80.0}
        layout = compose_layout("System looks healthy.", tool_data=data)
        types = [w.type for w in layout.widgets]
        assert "metric" in types
        metrics = [w for w in layout.widgets if isinstance(w, MetricWidget)]
        labels = {m.label for m in metrics}
        assert "CPU" in labels
        assert "RAM" in labels
        assert "Disk" in labels

    def test_system_info_with_gpu(self):
        data = {"cpu_pct": 10, "ram_pct": 20, "gpu_pct": 90}
        layout = compose_layout("", tool_data=data)
        gpu = next(
            (w for w in layout.widgets if isinstance(w, MetricWidget) and w.label == "GPU"),
            None,
        )
        assert gpu is not None
        assert gpu.value == "90"

    def test_system_info_leftover_fields_go_to_key_value(self):
        data = {"cpu_pct": 50, "hostname": "kali", "uptime": "5 days"}
        layout = compose_layout("", tool_data=data)
        kv = next((w for w in layout.widgets if isinstance(w, KeyValueWidget)), None)
        assert kv is not None
        labels = [p["label"] for p in kv.pairs]
        assert "hostname" in labels

    def test_system_info_summary_appended(self):
        data = {"cpu_pct": 10}
        layout = compose_layout("Everything nominal.", tool_data=data)
        md = next((w for w in layout.widgets if isinstance(w, MarkdownWidget)), None)
        assert md is not None
        assert "nominal" in md.text

    def test_search_results_produce_cards(self):
        results = [
            {"title": "Kali Linux", "url": "https://kali.org",
             "content": "Pen-testing distro"},
            {"title": "Offensive Security", "url": "https://offensive-security.com",
             "content": "OffSec"},
        ]
        layout = compose_layout("Here are the results.", tool_data=results)
        cards_w = next((w for w in layout.widgets if isinstance(w, CardsWidget)), None)
        assert cards_w is not None
        assert len(cards_w.items) == 2
        assert cards_w.items[0].title == "Kali Linux"

    def test_search_results_capped_at_8(self):
        results = [{"title": f"Result {i}", "url": f"http://r{i}.com"} for i in range(15)]
        layout = compose_layout("", tool_data=results)
        cards_w = next((w for w in layout.widgets if isinstance(w, CardsWidget)), None)
        assert cards_w is not None
        assert len(cards_w.items) == 8

    def test_search_results_summary_prepended(self):
        results = [{"title": "A", "url": "http://a.com"}]
        layout = compose_layout("Found one result.", tool_data=results)
        assert isinstance(layout.widgets[0], MarkdownWidget)
        assert "Found" in layout.widgets[0].text

    def test_flat_dict_produces_key_value(self):
        data = {"name": "Sarjan", "city": "Toronto", "role": "SWE"}
        layout = compose_layout("Here's the info.", tool_data=data)
        kv = next((w for w in layout.widgets if isinstance(w, KeyValueWidget)), None)
        assert kv is not None
        labels = {p["label"] for p in kv.pairs}
        assert "name" in labels

    def test_code_block_answer_produces_code_widget(self):
        text = "```python\ndef greet(name):\n    return f'Hello {name}'\n```"
        layout = compose_layout(text)
        assert len(layout.widgets) == 1
        assert isinstance(layout.widgets[0], CodeWidget)
        assert layout.widgets[0].language == "python"
        assert "greet" in layout.widgets[0].text

    def test_plain_prose_falls_back_to_markdown_without_llm(self):
        # When litellm is not importable and no structured data, falls back to markdown
        with patch.dict("sys.modules", {"litellm": None}):
            layout = compose_layout("This is just a plain answer with no structure.")
        assert len(layout.widgets) == 1
        assert isinstance(layout.widgets[0], MarkdownWidget)

    def test_no_tool_data_no_code_falls_back(self):
        layout = compose_layout("Some plain text.", tool_data=None)
        # Either LLM compose worked (multiple widgets) or fell back to markdown
        assert len(layout.widgets) >= 1
        # The widgets list must be non-empty and valid
        valid_types = {"markdown", "metric", "key_value", "code", "bar_chart", "cards", "table"}
        for w in layout.widgets:
            assert w.type in valid_types


# ── LLM compose path (mocked) ────────────────────────────────────────────────


class TestLLMComposePath:
    def _make_mock_litellm(self, json_str: str) -> MagicMock:
        """Build a litellm module mock that returns json_str as the response content."""
        mock_msg = MagicMock()
        mock_msg.content = json_str
        mock_choice = MagicMock()
        mock_choice.message = mock_msg
        mock_resp = MagicMock()
        mock_resp.choices = [mock_choice]
        mock_litellm = MagicMock()
        mock_litellm.completion.return_value = mock_resp
        return mock_litellm

    def test_valid_llm_json_produces_layout(self):
        payload = (
            '[{"type":"markdown","text":"Hello from LLM"},'
            '{"type":"metric","label":"Score","value":"99","unit":"pt"}]'
        )
        mock_ll = self._make_mock_litellm(payload)
        with patch.dict("sys.modules", {"litellm": mock_ll}):
            layout = compose_layout("Some prose answer.")
        types = [w.type for w in layout.widgets]
        assert "markdown" in types
        assert "metric" in types

    def test_invalid_json_falls_back_to_markdown(self):
        mock_ll = self._make_mock_litellm("This is not JSON at all!!!!")
        with patch.dict("sys.modules", {"litellm": mock_ll}):
            layout = compose_layout("Some plain answer.")
        # Should produce a markdown fallback
        assert len(layout.widgets) >= 1
        # At least one markdown widget exists
        assert any(isinstance(w, MarkdownWidget) for w in layout.widgets)

    def test_llm_returns_object_not_array_falls_back(self):
        mock_ll = self._make_mock_litellm('{"type":"markdown","text":"oops not an array"}')
        with patch.dict("sys.modules", {"litellm": mock_ll}):
            layout = compose_layout("An answer.")
        # _llm_compose returns None → falls back to markdown
        assert any(isinstance(w, MarkdownWidget) for w in layout.widgets)

    def test_llm_exception_falls_back_gracefully(self):
        mock_ll = MagicMock()
        mock_ll.completion.side_effect = RuntimeError("network error")
        with patch.dict("sys.modules", {"litellm": mock_ll}):
            layout = compose_layout("An answer.")
        assert any(isinstance(w, MarkdownWidget) for w in layout.widgets)

    def test_llm_returns_fenced_json_still_parses(self):
        payload = '```json\n[{"type":"markdown","text":"fenced"}]\n```'
        mock_ll = self._make_mock_litellm(payload)
        with patch.dict("sys.modules", {"litellm": mock_ll}):
            layout = compose_layout("An answer.")
        md = next((w for w in layout.widgets if isinstance(w, MarkdownWidget)), None)
        assert md is not None

    def test_deterministic_wins_over_llm_for_system_info(self):
        """system_info dict must NEVER call LLM — deterministic path wins."""
        mock_ll = MagicMock()
        data = {"cpu_pct": 55, "ram_pct": 70}
        with patch.dict("sys.modules", {"litellm": mock_ll}):
            layout = compose_layout("Info:", tool_data=data)
        mock_ll.completion.assert_not_called()
        assert any(isinstance(w, MetricWidget) for w in layout.widgets)

    def test_deterministic_wins_over_llm_for_search(self):
        """search results dict must NOT call LLM."""
        mock_ll = MagicMock()
        results = [{"title": "A", "url": "http://a.com"}]
        with patch.dict("sys.modules", {"litellm": mock_ll}):
            layout = compose_layout("Results:", tool_data=results)
        mock_ll.completion.assert_not_called()
        assert any(isinstance(w, CardsWidget) for w in layout.widgets)
