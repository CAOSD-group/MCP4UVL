"""
UVL MCP Server
==============

Model Context Protocol (MCP) server that grounds a general-purpose LLM in the
Universal Variability Language (UVL). It exposes:

  * Knowledge retrieval : official UVL grammar/documentation and few-shot
                          (description, UVL model) examples.
  * Validation          : parsing and automated analysis with flamapy
                          (satisfiability, number of configurations, dead
                          features, core features, false-optional features).
  * Persistence         : serialization of validated models to disk.

The LLM drafts a model, submits it for validation, reads the diagnostics and
repairs its own output until all checks pass.
"""

import sys
import os
import tempfile
import logging
from datetime import datetime

import requests
from mcp.server.fastmcp import FastMCP
from flamapy.metamodels.fm_metamodel.transformations import UVLReader
from flamapy.metamodels.pysat_metamodel.transformations import FmToPysat
from flamapy.metamodels.bdd_metamodel.transformations import FmToBDD
from flamapy.metamodels.bdd_metamodel.operations import BDDConfigurationsNumber
from flamapy.metamodels.pysat_metamodel.operations import (
    PySATSatisfiable,
    PySATDeadFeatures,
    PySATCoreFeatures,
    PySATFalseOptionalFeatures,
)

# The directory containing this file is the base path for all stored artifacts
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

EXAMPLES_DIR = os.path.join(BASE_DIR, "few_shot_examples")
GENERATED_DIR = os.path.join(BASE_DIR, "generated_models")
DEFICIENT_DIR = os.path.join(BASE_DIR, "deficient_models")

# Server initialization
mcp = FastMCP("UVL_Variability_Server")

# Official UVL resources used to ground the LLM
UVL_GRAMMAR_URL = "https://raw.githubusercontent.com/Universal-Variability-Language/uvl-parser/main/uvl/UVLParser.g4"
UVL_README_URL = "https://raw.githubusercontent.com/Universal-Variability-Language/uvl-parser/main/README.md"

# Characters that LLMs often emit but that the UVL parser does not accept
CHAR_REPLACEMENTS = {
    'á': 'a', 'é': 'e', 'í': 'i', 'ó': 'o', 'ú': 'u',
    'Á': 'A', 'É': 'E', 'Í': 'I', 'Ó': 'O', 'Ú': 'U',
    'ñ': 'n', 'Ñ': 'N', 'ü': 'u', 'Ü': 'U',
    '—': '-', '–': '-',
    '→': '=>', '->': '=>',
}


def _sanitize_uvl(uvl_code: str) -> str:
    """Replace accented characters, long dashes and arrows with ASCII equivalents."""
    for char, replacement in CHAR_REPLACEMENTS.items():
        uvl_code = uvl_code.replace(char, replacement)
    return uvl_code


# TOOL 1: Few-shot examples (knowledge retrieval)
@mcp.tool()
def get_uvl_examples() -> str:
    """
    Provides correct (description, UVL model) example pairs.
    ALWAYS call this tool before generating any code to understand
    how natural-language descriptions are translated into valid UVL.
    """
    if not os.path.exists(EXAMPLES_DIR):
        return f"Warning: the examples directory was not found. The folder must be located at: {EXAMPLES_DIR}"

    compiled_examples = []

    try:
        for filename in sorted(os.listdir(EXAMPLES_DIR)):
            if filename.endswith(".txt"):
                base_name = filename[:-4]
                txt_path = os.path.join(EXAMPLES_DIR, filename)
                uvl_path = os.path.join(EXAMPLES_DIR, f"{base_name}.uvl")

                with open(txt_path, "r", encoding="utf-8") as f:
                    description = f.read().strip()

                uvl_code = "The associated UVL model was not found."
                if os.path.exists(uvl_path):
                    with open(uvl_path, "r", encoding="utf-8") as f:
                        uvl_code = f.read().strip()

                block = f"""
### EXAMPLE: {base_name.upper()} ###
--- DESCRIPTION ---
{description}

--- EXPECTED UVL CODE ---
{uvl_code}
---------------------------------------
"""
                compiled_examples.append(block)

        if not compiled_examples:
            return "The examples directory is empty."

        return "Here are reference examples of descriptions and their correct UVL models:\n\n" + "\n".join(compiled_examples)

    except Exception as e:
        return f"Error while processing the examples directory: {str(e)}"


# TOOL 2: Official UVL grammar and documentation (knowledge retrieval)
@mcp.tool()
def get_uvl_grammar_rules() -> str:
    """
    Downloads the official UVL grammar and language documentation.
    Always use it before generating models.
    """
    try:
        # Request the grammar and the README of the official UVL repository
        r_grammar = requests.get(UVL_GRAMMAR_URL, timeout=10)
        r_grammar.raise_for_status()
        r_readme = requests.get(UVL_README_URL, timeout=10)
        readme = r_readme.text if r_readme.status_code == 200 else "README not available."

        return f"""
        --- UVL CONTEXT (README of the official UVL repository) ---
        {readme}
        --- FORMAL GRAMMAR ---
        {r_grammar.text}
        """
    except Exception as e:
        return f"Error while retrieving the grammar: {str(e)}"


# TOOL 3: Syntactic and semantic validation with flamapy
@mcp.tool()
def validate_uvl_syntax_sat_bdd(uvl_code: str) -> str:
    """
    Validates the syntax of a UVL model using flamapy.
    Once the model parses, it also checks its satisfiability and reports
    the number of valid configurations, core features, dead features and
    false-optional features.
    The LLM must ALWAYS call this tool after generating a model.
    """

    # Strip Markdown artifacts (code fences) that the model may add
    uvl_code = uvl_code.strip()
    if uvl_code.startswith("```"):
        lines = uvl_code.split("\n")
        end = -1 if lines[-1].strip().startswith("```") else len(lines)
        uvl_code = "\n".join(lines[1:end])
    if uvl_code.startswith("uvl\n"):
        uvl_code = uvl_code[4:]

    # Write the candidate to a temporary file so that flamapy can read it
    temp_dir = tempfile.gettempdir()
    uvl_path = os.path.join(temp_dir, "claude_temp_model.uvl")

    try:
        with open(uvl_path, 'w', encoding='utf-8') as f:
            f.write(uvl_code)

        # Temporarily disable logging so it does not interfere with the MCP channel
        logging.disable(logging.CRITICAL)

        # Parse the candidate model with flamapy
        reader = UVLReader(uvl_path)
        model = reader.transform()

        # Semantic analysis (SAT and BDD) if the syntax is correct
        sat_result = None
        num_configs = None
        analysis_report = ""
        has_anomalies = False

        if model is not None:
            try:
                # SAT analysis: is the model satisfiable?
                sat_model = FmToPysat(model).transform()
                sat_result = PySATSatisfiable().execute(sat_model).get_result()

                if sat_result:
                    # Dead features (never selectable), core features (always present)
                    # and false-optional features (declared optional but always required)
                    dead_feats = PySATDeadFeatures().execute(sat_model).get_result()
                    core_feats = PySATCoreFeatures().execute(sat_model).get_result()
                    false_opts = PySATFalseOptionalFeatures().execute(sat_model).get_result()

                    dead_names = [str(f) for f in dead_feats] if dead_feats else []
                    core_names = [str(f) for f in core_feats] if core_feats else []
                    false_opt_names = [str(f) for f in false_opts] if false_opts else []

                    if dead_names or false_opt_names:
                        has_anomalies = True

                    # BDD analysis (number of configurations), only if satisfiable
                    bdd_model = FmToBDD(model).transform()
                    num_configs = BDDConfigurationsNumber().execute(bdd_model).get_result()

                    analysis_report = f"""
--- SEMANTIC AUDIT REPORT ---
- Total valid configurations: {num_configs}
- Core features (always present, due to mandatory relations or constraints): {', '.join(core_names) if core_names else 'None'}
- Dead features: {', '.join(dead_names) if dead_names else 'None (Perfect)'}
- False-optional features: {', '.join(false_opt_names) if false_opt_names else 'None (Perfect)'}
---------------------------------------
                    """

            except ImportError:
                pass  # If the analysis libraries are missing, skip silently so the base validator keeps working

        # Restore logging
        logging.disable(logging.NOTSET)

        # Evaluate the outcome
        if model is None:
            msg = getattr(reader, 'get_error_message', lambda: "Unknown parser error")()
            return f"Validation failed. Flamapy detected the following syntax error:\n{msg}\nPlease fix it and validate again."

        if sat_result is False:
            return "Semantic validation failed: the model has 0 valid configurations because of contradictory constraints (void model). Analyze the exclusions and fix them."

        if sat_result is True:
            # Check for design anomalies (dead or false-optional features)
            if has_anomalies:
                os.makedirs(DEFICIENT_DIR, exist_ok=True)
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                deficient_filename = f"deficient_model_{timestamp}.uvl"
                filepath = os.path.join(DEFICIENT_DIR, deficient_filename)
                uvl_code = _sanitize_uvl(uvl_code)
                try:
                    with open(filepath, "w", encoding="utf-8") as f:
                        f.write(uvl_code)
                    saved_msg = f"\n[System note: this intermediate model was automatically saved as 'deficient_models/{deficient_filename}' for research purposes]."
                except Exception:
                    saved_msg = ""

                return f"Validation Warning: the model is satisfiable, but it contains serious design anomalies.\n{analysis_report}{saved_msg}\nRead the report, reason about why these anomalies occur, fix the UVL code and validate it again until it has no anomalies."

            else:
                return f"Validation Success: the model is logically sound.\n{analysis_report}"
    except Exception as e:
        logging.disable(logging.NOTSET)
        return f"Validation failed: internal syntax error detected:\n{str(e)}"
    finally:
        # Clean up temporary files
        if os.path.exists(uvl_path):
            try:
                os.remove(uvl_path)
            except OSError:
                pass


# TOOL 4: Persistence of validated models
@mcp.tool()
def save_valid_model(uvl_code: str, filename: str) -> str:
    """
    Saves the validated UVL model to disk.
    Only call it if the validator has returned Success.
    """
    uvl_code = _sanitize_uvl(uvl_code)

    try:
        os.makedirs(GENERATED_DIR, exist_ok=True)
        if not filename.endswith(".uvl"):
            filename += ".uvl"

        filepath = os.path.join(GENERATED_DIR, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(uvl_code)

        return f"Success: model saved to {filepath}"
    except Exception as e:
        return f"Permission error while saving the file: {str(e)}"


# MAIN
if __name__ == "__main__":
    print("Starting UVL MCP server...", file=sys.stderr)
    mcp.run()
