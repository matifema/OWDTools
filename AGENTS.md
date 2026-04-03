# AGENTS.md: Codebase Guide and Conventions

Welcome to the OWID Data Tools codebase! This guide provides essential context, rules, and style conventions for AI agents operating in this repository. 

## 1. Project Overview
This repository contains an Open WebUI-compatible tool plugin (`owid_tools.py`) for searching, fetching, and visualizing data from Our World in Data (OWID). It uses `pandas` for data manipulation, `owid-catalog` for fetching data, and embeds official OWID interactive charts via iframes.

## 2. Build, Lint, and Test Commands

Currently, the project is a single-file Python module. However, the following standard conventions should be used when extending the codebase or running checks:

- **Linting & Formatting:** Use `ruff` if available or standard `flake8`/`black`. 
  *Command:* `ruff check .` or `black .`
- **Type Checking:** Use `mypy`.
  *Command:* `mypy owid_tools.py`
- **Testing:** There is no dedicated test suite yet. When adding tests, use `pytest`.
  *Run all tests:* `pytest`
  *Run a single test file:* `pytest tests/test_owid_tools.py`
  *Run a specific test function:* `pytest tests/test_owid_tools.py::test_specific_function`

## 3. Code Style and Conventions

### 3.1 Imports
- Always include `from __future__ import annotations` at the top of Python files.
- Group imports logically: standard library first, third-party packages next, and finally local modules.
- Handle optional dependencies gracefully using `try/except ImportError` blocks (e.g., `owid.catalog`, `fastapi.responses`).

### 3.2 Typing and Naming
- Use standard Python type hints for all function arguments and return values (`typing.List`, `typing.Optional`, `typing.Any`).
- Helper functions should be prefixed with an underscore (e.g., `_detect_cols`, `_clean_series`).
- Variable and function names should be `snake_case`. Classes should be `PascalCase`.

### 3.3 Formatting
- Keep code clean and readable, loosely following PEP8.
- Use double quotes `"` for strings and docstrings.
- Ensure clear, multi-line docstrings for any public method (especially tool endpoints), explaining arguments clearly. 

### 3.4 Error Handling
- Tool methods (e.g., `search_owid`, `chart_owid_data`) **should not raise exceptions** directly to the caller. 
- Instead, catch exceptions and return a clear, user-friendly error string.
  *Example:*
  ```python
  try:
      table = fetch(slug)
  except Exception as e:
      return f"Error fetching '{slug}': {e}"
  ```

### 3.5 Specific Design Patterns
- **Valves:** Use Pydantic's `BaseModel` for configuration parameters (Valves).
- **Data manipulation:** Drop NaN values instead of filling with zeroes to prevent charting artifacts. Use `pandas` safely, ensuring types are coerced properly (e.g., year columns cast to integers).
- **Visuals:** Visualizations rely on official OWID grapher iframes. Ensure HTML snippets map correctly to chart sizes based on user preferences.

## 4. Workflows for Modifications
- When modifying data fetching logic, ensure dataframe manipulation handles edge cases smoothly.
- Before claiming a task is done, ensure `owid_tools.py` has valid syntax by running `python -m py_compile owid_tools.py`.
- Do not introduce heavy new dependencies unless strictly necessary. Rely on `pandas` and built-in Python libraries.