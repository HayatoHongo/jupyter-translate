import json
import os
import re
import sys
import argparse
from tqdm import tqdm  # For progress bar
from time import sleep

# ---- 追加インポート（ファイル冒頭付近に） -----------------
import openai
import backoff
import dotenv
import logging
import time
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()  # .env があれば自動で環境変数に反映
# -----------------------------------------------------------

# --- Configure logging  -----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

# 2) このモジュール (__name__) のみ DEBUG
logging.getLogger(__name__).setLevel(logging.DEBUG)

# 3) 外部ライブラリのログは WARNING 以上に引き上げ
logging.getLogger("openai").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("http.client").setLevel(logging.WARNING)
logging.getLogger("backoff").setLevel(logging.WARNING)

# Load prompts
_PROMPTS_PATH = Path(__file__).parent / "prompts.json"
try:
    with open(_PROMPTS_PATH, encoding="utf-8") as f:
        PROMPTS = json.load(f)
except Exception as e:
    logging.error(f"Failed to load prompts from {_PROMPTS_PATH}: {e}")
    raise

# Helper: translate code comments / print strings via OpenAI API
def translate_code_text(text: str,
                        dest_language: str,
                        model: str = "gpt-4.1-mini") -> str:
    """
    Uses ChatGPT to translate a single comment or string literal into dest_language.
    """
    openai.api_key = os.getenv("OPENAI_API_KEY")
    if not openai.api_key:
        raise RuntimeError("OPENAI_API_KEY environment variable not set")

    system_lines = PROMPTS.get("code_translation_system_prompt_lines")
    if not system_lines:
        raise RuntimeError("'code_translation_system_prompt_lines' not found in prompts.json")

    system_msg = "\n".join(system_lines)
    messages = [
        {"role": "system", "content": system_msg},
        {"role": "user",   "content": f"Translate into {dest_language}:\n\n{text}"}
    ]

    @backoff.on_exception(
        backoff.expo,
        (openai.error.RateLimitError, openai.error.APIError, openai.error.Timeout),
        max_tries=3
    )
    def _call():
        return openai.ChatCompletion.create(
            model=model,
            messages=messages,
            temperature=0.7
        )

    resp = _call()
    return resp.choices[0].message.content.strip()


def translate_markdown(text: str,
                       *_,
                       delay: int,
                       dest_language: str,
                       model: str = "gpt-4.1-mini"
                      ) -> str:
    """
    Translate one Markdown cell with ChatGPT.
    """
    if not text.strip():
        return text

    image_only_pattern = re.compile(
        r'^(?:!\[[^\]]*\]\((?:data:image/[^)]+|attachment:[^)]+)\)\s*)+$'
    )
    if image_only_pattern.match(text.strip()):
        return text

    openai.api_key = os.getenv("OPENAI_API_KEY")
    if not openai.api_key:
        raise RuntimeError("OPENAI_API_KEY environment variable not set")

    @backoff.on_exception(
        backoff.expo,
        (openai.error.RateLimitError, openai.error.APIError, openai.error.Timeout),
        max_tries=3
    )
    def request_translation(content: str) -> str:
        raw = PROMPTS["translation_system_prompt_lines"]
        system_msg = "\n".join(raw)
        if not system_msg:
            logging.error("Missing 'translation_system_prompt_lines' in prompts.json")
            raise RuntimeError("translation_system_prompt_lines not found in prompts.json")

        messages = [
            {"role": "system", "content": system_msg},
            {"role": "user",   "content": f"Translate into {dest_language}:\n\n{content}"}
        ]
        resp = openai.ChatCompletion.create(
            model=model,
            messages=messages,
            temperature=1.0
        )
        return resp.choices[0].message.content.strip()

    translated = request_translation(text)
    if text.endswith("\n") and not translated.endswith("\n"):
        translated += "\n"
    return translated


# 変換時のデバッグログを有効化
# logging.getLogger().setLevel(logging.DEBUG)


def translate_code_comments_and_prints(code: str,
                                      dest_language: str,
                                      model: str = "gpt-4.1-mini") -> str:
    """
    Translate comments, docstrings, and simple print statements in code.
    Uses translate_code_text() for each piece of text.
    """
    # logging.debug("Starting translate_code_comments_and_prints")
    if not code.strip():
        return code

    def translate_text(txt):
        # logging.debug(f"Translating text: '{txt[:30]}...'")
        res = translate_code_text(txt, dest_language=dest_language, model=model)
        # logging.debug(f"Result: '{res[:30]}...'")
        return res

    lines = code.split("\n")
    out = []
    i = 0
    total = len(lines)

    while i < total:
        line = lines[i]

        # Docstring handling
        m = re.match(r"(\s*)(?P<delim>'''|\"\"\")", line)
        if m:
            indent, delim = m.group(1), m.group("delim")
            stripped = line.strip()
            # Single-line docstring?
            if stripped.endswith(delim) and len(stripped) > len(delim)*2 - 2:
                content = stripped[len(delim):-len(delim)]
                out.append(f"{indent}{delim}{translate_text(content)}{delim}")
                i += 1
                continue
            # Multi-line
            out.append(line)
            i += 1
            buffer = []
            while i < total and not lines[i].strip().endswith(delim):
                buffer.append(lines[i])
                i += 1
            joined = "\n".join(buffer)
            translated = translate_text(joined)
            for tline in translated.split("\n"):
                out.append(f"{indent}{tline}")
            if i < total:
                out.append(lines[i])
            i += 1
            continue

        # Inline comment
        if "#" in line:
            code_part, comment = line.split("#", 1)
            comment = comment.strip()
            todo = re.match(r"(TODO:)(.*)", comment, re.IGNORECASE)
            if todo:
                prefix, rest = todo.groups()
                new_comment = f"{prefix} {translate_text(rest.strip())}"
            else:
                new_comment = translate_text(comment)
            out.append(f"{code_part}# {new_comment}")
            i += 1
            continue

        # Skip f-string prints
        if "print(f" in line:
            out.append(line)
            i += 1
            continue

        # Regular print(...)
        pm = re.search(r'print\(\s*("|\')(.+?)\1', line)
        if pm and "print_formatted_tensor" not in line:
            quote, txt = pm.group(1), pm.group(2)
            new_txt = translate_text(txt)
            out.append(line.replace(f"{quote}{txt}{quote}", f"{quote}{new_txt}{quote}"))
            i += 1
            continue

        # print_formatted_tensor(...)
        tm = re.search(r'print_formatted_tensor\(\s*("|\')(.+?)\1', line)
        if tm:
            quote, txt = tm.group(1), tm.group(2)
            new_txt = translate_text(txt)
            out.append(line.replace(f"{quote}{txt}{quote}", f"{quote}{new_txt}{quote}"))
            i += 1
            continue

        # Default
        out.append(line)
        i += 1

    # logging.debug("Completed translate_code_comments_and_prints")
    return "\n".join(out)


def jupyter_translate(fname, src_language, dest_language, delay,
                      rename_source_file=False, print_translation=False):
    """
    Translates a Jupyter Notebook from one language to another.
    """
    with open(fname, 'r', encoding='utf-8') as f:
        nb = json.load(f)

    total = len(nb['cells'])
    code_cells = sum(1 for c in nb['cells'] if c['cell_type'] == 'code')
    md_cells = sum(1 for c in nb['cells'] if c['cell_type'] == 'markdown')
    print(f"Total cells: {total}, code: {code_cells}, markdown: {md_cells}")

    for i, cell in enumerate(tqdm(nb['cells'], desc="Translating cells")):
        if cell['cell_type'] == 'markdown':
            full = ''.join(cell['source'])
            trans = translate_markdown(full, None,
                                       delay=delay,
                                       dest_language=dest_language)
            nb['cells'][i]['source'] = trans.splitlines(True)
            if print_translation:
                print(f"MD cell {i}:\n{trans}")

        elif cell['cell_type'] == 'code':
            translated_src = [
                translate_code_comments_and_prints(
                    line,
                    dest_language=dest_language
                )
                for line in cell['source']
            ]
            nb['cells'][i]['source'] = translated_src
            if print_translation:
                print(f"Code cell {i}:\n{''.join(translated_src)}")

    base, ext = os.path.splitext(fname)
    out_fname = f"{base}_{dest_language}{ext}"
    with open(out_fname, 'w', encoding='utf-8') as f:
        json.dump(nb, f, ensure_ascii=False, indent=2)
    print(f"Saved translated notebook to: {out_fname}")


def translate_directory(directory, src_language, dest_language, delay,
                        rename_source_file=False, print_translation=False,
                        recursive=True):
    if not os.path.isdir(directory):
        print(f"Error: {directory} is not a valid directory")
        return

    count = 0
    walker = os.walk(directory) if recursive else [(directory, [], os.listdir(directory))]
    for root, _, files in walker:
        for fn in files:
            if fn.endswith('.ipynb'):
                path = os.path.join(root, fn)
                print(f"Translating {path}...")
                jupyter_translate(path, src_language, dest_language,
                                  delay,
                                  rename_source_file,
                                  print_translation)
                count += 1
    print(f"Translated {count} notebook{'s' if count != 1 else ''}.")


def main():
    parser = argparse.ArgumentParser(
        description="Translate a Jupyter Notebook from one language to another."
    )
    parser.add_argument('fname', help="Notebook file or directory")
    parser.add_argument('--source', default='auto', help="Source language code")
    parser.add_argument('--target', required=True, help="Destination language code")
    parser.add_argument('--delay', type=int, default=10, help="API retry delay (s)")
    parser.add_argument('--print', dest='print_translation',
                        action='store_true', help="Print translations")
    parser.add_argument('--directory', action='store_true',
                        help="Process all .ipynb in directory")
    parser.add_argument('--no-recursive', dest='recursive',
                        action='store_false', help="Disable subdirs")
    parser.set_defaults(recursive=True)

    args = parser.parse_args()
    src = args.source.lower()
    tgt = args.target.lower()
    print(f"Translating from {src} to {tgt}")

    if args.directory or os.path.isdir(args.fname):
        translate_directory(args.fname, src, tgt, args.delay,
                            print_translation=args.print_translation,
                            recursive=args.recursive)
    else:
        jupyter_translate(args.fname, src, tgt, args.delay,
                          print_translation=args.print_translation)


if __name__ == '__main__':
    main()
