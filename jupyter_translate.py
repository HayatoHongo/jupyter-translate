import json, os, re, sys
import argparse
from deep_translator import (
    GoogleTranslator,
    MyMemoryTranslator
)
from tqdm import tqdm  # For progress bar
from time import sleep


# ---- 追加インポート（ファイル冒頭付近に） -----------------
import openai, backoff, os, time, dotenv, logging
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()              # .env があれば自動で環境変数に反映
# -----------------------------------------------------------


# --- Add configure logging  -----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

# Add load prompts
_PROMPTS_PATH = Path(__file__).parent / "prompts.json"
try:
    with open(_PROMPTS_PATH, encoding="utf-8") as f:
        PROMPTS = json.load(f)
except Exception as e:
    logging.error(f"Failed to load prompts from {_PROMPTS_PATH}: {e}")
    raise
# --------------------------------------------

# Função para selecionar o tradutor com base no nome
def get_translator(translator_name, src_language, dest_language):
    translators = {
        'google': GoogleTranslator,
        'mymemory': MyMemoryTranslator,
    }
    TranslatorClass = translators.get(translator_name.lower())
    if not TranslatorClass:
        raise ValueError(f"Translator {translator_name} not supported.")
    
    try:
        print(f"Using translator: {translator_name.capitalize()}")
        print(f"Source language: {src_language}, Target language: {dest_language}")
        
        # Get supported languages
        supported_languages = TranslatorClass().get_supported_languages(as_dict=True)
        
        # Check if source and target languages are supported
        if src_language not in supported_languages.values():
            print(f"Warning: Source language '{src_language}' might not be supported. Available languages:")
            for code, lang in supported_languages.items():
                if src_language.lower() in lang.lower():
                    print(f"  - Did you mean '{lang}' (code: {code})?")
        
        if dest_language not in supported_languages.values():
            print(f"Warning: Target language '{dest_language}' might not be supported. Available languages:")
            for code, lang in supported_languages.items():
                if dest_language.lower() in lang.lower():
                    print(f"  - Did you mean '{lang}' (code: {code})?")
        
        # Initialize translator with source and target languages
        return TranslatorClass(source=src_language, target=dest_language)
        
    except Exception as e:
        if 'No support for the provided language' in str(e):
            print(f"Error: {e}")
            supported_languages = TranslatorClass().get_supported_languages(as_dict=True)
            print(f"Supported languages for {translator_name}: {supported_languages}")
        else:
            print(f"Error initializing the translator: {e}")
        sys.exit(1)

def safe_translate(translator, text, retries=3, delay=10):
    if not text.strip():  # Skip empty texts
        return text
        
    print(f"Translating text: {text[:30]}...")  # Debug: Show what we're translating
    for i in range(retries):
        try:
            translated = translator.translate(text)
            print(f"Translation result: {translated[:30]}...")  # Debug: Show result
            return translated
        except Exception as e:
            print(f"Error translating: {str(e)}. Trying again ({i+1}/{retries})...")
            sleep(delay)
    raise Exception(f"Failed to translate after {retries} attempts.")

def translate_markdown(text: str,
                       translator,       # unused, for compatibility
                       delay: int,
                       dest_language: str,
                       model: str = "gpt-4.1-mini"
                      ) -> str:
    """
    Translate one Markdown cell as-is with ChatGPT.
    Uses the destination language in the prompt, skipping pure-image cells.
    """
    # 空セルはそのまま返却
    if not text.strip():
        return text

    # 画像だけのセルはスキップ
    image_only_pattern = re.compile(
        r'^(?:!\[[^\]]*\]\((?:data:image/[^)]+|attachment:[^)]+)\)\s*)+$'
    )
    if image_only_pattern.match(text.strip()):
        return text

    # OpenAI APIキー の取得と確認
    openai.api_key = os.getenv("OPENAI_API_KEY")
    if not openai.api_key:
        raise RuntimeError("OPENAI_API_KEY environment variable not set")

    # ChatGPT へ再試行付きリクエストを送信
    @backoff.on_exception(
        backoff.expo,
        (openai.error.RateLimitError, openai.error.APIError, openai.error.Timeout),
        max_tries=3
    )
    def request_translation(content: str) -> str:
        # Look up the system prompt
        raw = PROMPTS["translation_system_prompt_lines"]
        system_msg = "\n".join(raw)

        if not system_msg:
            # Log an error—and raise so you don’t send an empty or incorrect system prompt
            logging.error(
                "Missing 'translation_system_prompt' in prompts.json; "
                "please add this key with your system instructions."
            )
            raise RuntimeError("translation_system_prompt not found in prompts.json")

        messages = [
            {"role": "system",  "content": system_msg},
            {"role": "user",    "content": f"Translate into {dest_language}:\n\n{content}"}
        ]
        logging.debug(f"Calling OpenAI with dest_language={dest_language}")
        resp = openai.ChatCompletion.create(
            model=model,
            messages=messages,
            temperature=1.0
        )
        return resp.choices[0].message.content.strip()

    # 翻訳実行
    translated = request_translation(text)

    # 元のセル末尾改行を維持
    if text.endswith("\n") and not translated.endswith("\n"):
        translated += "\n"

    return translated


import re
import logging

# 変換時のデバッグログを有効化
logging.basicConfig(level=logging.DEBUG)

def translate_code_comments_and_prints(code, translator, delay):
    """
    Translate comments, docstrings, and simple print statements in code with lookahead for docstrings.
    Handles:
      - '''...'''  blocks (multi-line and single-line)
      - Inline comments (# ...), preserving TODO: prefix
      - print(...)
      - print_formatted_tensor(label, tensor)
      - Skips f-string prints (print(f"..."))
    """
    logging.debug("Starting translate_code_comments_and_prints")
    if not code or code.isspace():
        return code

    def translate_text(text):
        logging.debug(f"Translating text: '{text[:30]}...'")
        result = safe_translate(translator, text, delay=delay)
        logging.debug(f"Translation result: '{result[:30]}...'")
        return result

    lines = code.split('\n')
    result_lines = []
    i = 0
    total = len(lines)

    while i < total:
        line = lines[i]
        logging.debug(f"Processing line {i}: {line}")

        # --- Docstring detection with lookahead ---
        start_match = re.match(r"(\s*)(?P<delim>'''|\"\"\")", line)
        if start_match:
            indent = start_match.group(1)
            delim = start_match.group('delim')
            logging.debug(f"Docstring start at line {i} with delimiter {delim}")
            stripped = line.strip()
            # Single-line docstring? (text on same line)
            if stripped.endswith(delim) and len(stripped) > len(delim) * 2 - 2:
                content = stripped[len(delim):-len(delim)]
                translated = translate_text(content)
                result_lines.append(f"{indent}{delim}{translated}{delim}")
                i += 1
                continue
            # Multi-line docstring: collect inner lines
            result_lines.append(line)  # opening line
            i += 1
            buffer = []
            while i < total:
                current = lines[i]
                # closing delimiter detection
                if current.strip().endswith(delim):
                    break
                buffer.append(current)
                i += 1
            # Translate the buffered docstring content
            joined = '\n'.join(buffer)
            logging.debug(f"Docstring block to translate (lines {i-len(buffer)}-{i-1}): '''{joined}'''")
            translated_block = translate_text(joined)
            # Re-emit translated lines, preserving indent
            for translated_line in translated_block.split('\n'):
                result_lines.append(f"{indent}{translated_line}")
            # Append closing delimiter line
            if i < total:
                result_lines.append(lines[i])
                logging.debug(f"Docstring end at line {i}")
            i += 1
            continue

        # --- Inline comments ---
        if '#' in line:
            code_part, comment = line.split('#', 1)
            comment = comment.strip()
            logging.debug(f"Inline comment: {comment}")
            todo = re.match(r'(TODO:)(.*)', comment, re.IGNORECASE)
            if todo:
                prefix, rest = todo.groups()
                translated_rest = translate_text(rest.strip())
                new_comment = f"{prefix} {translated_rest}"
            else:
                new_comment = translate_text(comment)
            result_lines.append(f"{code_part}# {new_comment}")
            i += 1
            continue

        # --- Skip f-string prints ---
        if 'print(f' in line:
            result_lines.append(line)
            i += 1
            continue

        # --- Regular print(...) ---
        print_match = re.search(r'print\(\s*("|\')(.+?)\1', line)
        if print_match and 'print_formatted_tensor' not in line:
            quote, txt = print_match.group(1), print_match.group(2)
            translated_txt = translate_text(txt)
            new_line = line.replace(f"{quote}{txt}{quote}", f"{quote}{translated_txt}{quote}")
            result_lines.append(new_line)
            i += 1
            continue

        # --- print_formatted_tensor(...) ---
        tensor_match = re.search(r'print_formatted_tensor\(\s*("|\')(.+?)\1', line)
        if tensor_match:
            quote, txt = tensor_match.group(1), tensor_match.group(2)
            translated_txt = translate_text(txt)
            new_line = line.replace(f"{quote}{txt}{quote}", f"{quote}{translated_txt}{quote}")
            result_lines.append(new_line)
            i += 1
            continue

        # --- Default: unchanged ---
        result_lines.append(line)
        i += 1

    logging.debug("Completed translate_code_comments_and_prints")
    return '\n'.join(result_lines)



def jupyter_translate(fname, src_language, dest_language, delay, translator_name, rename_source_file=False, print_translation=False):
    """
    Translates a Jupyter Notebook from one language to another.
    """

    # Initialize the translator
    translator = get_translator(translator_name, src_language, dest_language)
    
    # Test the translator with a simple text
    test_text = "Teste de tradução. Isso deve ser traduzido."
    try:
        test_result = translator.translate(test_text)
        print(f"Translator test - Original: '{test_text}' → Translated: '{test_result}'")
    except Exception as e:
        print(f"Translator test failed: {str(e)}")
        print("The translator is not working correctly. Please check your settings and try again.")
        sys.exit(1)

    # Check if the necessary parameters are provided
    if not fname or not dest_language:
        print("Error: Missing required parameters.")
        print("Usage: python jupyter_translate.py <notebook_file> --source <source_language> --target <destination_language> --translator <translator>")
        sys.exit(1)

    # Load the notebook file
    with open(fname, 'r', encoding='utf-8') as file:
        data_translated = json.load(file)

    total_cells = len(data_translated['cells'])
    code_cells = sum(1 for cell in data_translated['cells'] if cell['cell_type'] == 'code')
    markdown_cells = sum(1 for cell in data_translated['cells'] if cell['cell_type'] == 'markdown')

    print(f"Total cells: {total_cells}")
    print(f"Code cells: {code_cells}")
    print(f"Markdown cells: {markdown_cells}")

    # Translate each cell
    for i, cell in enumerate(tqdm(data_translated['cells'], desc="Translating cells", unit="cell")):
        if cell['cell_type'] == 'markdown':
            # For markdown cells, we need to handle special markdown syntax
            # Join all source lines into a single string for better translation
            full_markdown = ''.join(cell['source'])
            
            # Translate the whole markdown content
            translated_markdown = translate_markdown(full_markdown, translator, delay=delay, dest_language=dest_language)
            
            # Split the translated content back into lines
            data_translated['cells'][i]['source'] = translated_markdown.splitlines(True)  # keepends=True to preserve newlines
            
            if print_translation:
                print(f"Translated markdown cell {i}:")
                print(''.join(data_translated['cells'][i]['source']))
                
        elif cell['cell_type'] == 'code':
            # For code cells, translate comments and print statements
            translated_source = []
            for source_line in cell['source']:
                # Translate comments and formatted print statements within code
                translated_line = translate_code_comments_and_prints(source_line, translator, delay=delay)
                translated_source.append(translated_line)
                
            data_translated['cells'][i]['source'] = translated_source
            
            if print_translation:
                print(f"Translated code cell {i}:")
                print(''.join(data_translated['cells'][i]['source']))

    if rename_source_file:
        fname_bk = f"{'.'.join(fname.split('.')[:-1])}_bk.ipynb"  # index.ipynb -> index_bk.ipynb

        os.rename(fname, fname_bk)
        print(f'{fname} has been renamed as {fname_bk}')

        with open(fname, 'w', encoding='utf-8') as f:
            json.dump(data_translated, f, ensure_ascii=False, indent=2)
        print(f'The {dest_language} translation has been saved as {fname}')
    else:
        dest_fname = f"{'.'.join(fname.split('.')[:-1])}_{dest_language}.ipynb"  # any.name.ipynb -> any.name_en.ipynb
        with open(dest_fname, 'w', encoding='utf-8') as f:
            json.dump(data_translated, f, ensure_ascii=False, indent=2)
        print(f'The {dest_language} translation has been saved as {dest_fname}')

def translate_directory(directory, src_language, dest_language, delay, translator_name, rename_source_file=False, print_translation=False, recursive=True):
    """
    Translates all Jupyter Notebooks in a directory.
    
    Args:
        directory (str): Path to the directory containing the notebooks
        src_language (str): Source language code
        dest_language (str): Destination language code
        delay (int): Delay between API calls to avoid rate limiting
        translator_name (str): Name of the translator to use
        rename_source_file (bool): Whether to rename the original file
        print_translation (bool): Whether to print translations to console
        recursive (bool): Whether to process subdirectories recursively
    """
    if not os.path.isdir(directory):
        print(f"Error: {directory} is not a valid directory")
        return

    translated_files = 0
    
    # Walk through the directory and its subdirectories if recursive is True
    if recursive:
        for root, _, files in os.walk(directory):
            for file in files:
                if file.endswith('.ipynb'):
                    notebook_path = os.path.join(root, file)
                    print(f"\nTranslating {notebook_path}...")
                    jupyter_translate(
                        fname=notebook_path, 
                        src_language=src_language,
                        dest_language=dest_language,
                        delay=delay,
                        translator_name=translator_name,
                        rename_source_file=rename_source_file,
                        print_translation=print_translation
                    )
                    translated_files += 1
    else:
        # Process only files in the current directory
        for file in os.listdir(directory):
            if file.endswith('.ipynb'):
                notebook_path = os.path.join(directory, file)
                print(f"\nTranslating {notebook_path}...")
                jupyter_translate(
                    fname=notebook_path, 
                    src_language=src_language,
                    dest_language=dest_language,
                    delay=delay,
                    translator_name=translator_name,
                    rename_source_file=rename_source_file,
                    print_translation=print_translation
                )
                translated_files += 1
    
    print(f"\nTranslation complete! Translated {translated_files} notebook{'s' if translated_files != 1 else ''}.")

# Main function to parse arguments and run the translation
def main():
    parser = argparse.ArgumentParser(description="Translate a Jupyter Notebook from one language to another.")
    parser.add_argument('fname', help="Path to the Jupyter Notebook file or directory containing notebooks")
    parser.add_argument('--source', default='auto', help="Source language code (default: auto-detect)")
    parser.add_argument('--target', required=True, help="Destination language code")
    parser.add_argument('--delay', type=int, default=10, help="Delay between retries in seconds (default: 10)")
    parser.add_argument('--translator', default='google', help="Translator to use (options: google or mymemory). Default: google")
    parser.add_argument('--rename', action='store_true', help="Rename the original file after translation")
    parser.add_argument('--print', dest='print_translation', action='store_true', help="Print translations to console")
    parser.add_argument('--directory', action='store_true', help="Process all .ipynb files in the specified directory")
    parser.add_argument('--no-recursive', dest='recursive', action='store_false', help="Don't process subdirectories when using --directory")
    parser.set_defaults(recursive=True)

    args = parser.parse_args()

    # Map common language names to ISO codes if full names are provided
    language_map = {
        'english': 'en',
        'portuguese': 'pt',
        'spanish': 'es',
        'french': 'fr',
        'german': 'de',
        'italian': 'it',
        'dutch': 'nl',
        'chinese': 'zh-CN',
        'japanese': 'ja',
        'korean': 'ko',
        'russian': 'ru',
        'arabic': 'ar'
    }

    # Convert source and target languages to ISO codes if they are full names
    src_language = args.source.lower()
    if src_language in language_map:
        src_language = language_map[src_language]
    
    dest_language = args.target.lower()
    if dest_language in language_map:
        dest_language = language_map[dest_language]

    print(f"Using source language code: {src_language}, target language code: {dest_language}")

    # Check if we're processing a directory or a single file
    if args.directory or os.path.isdir(args.fname):
        translate_directory(
            directory=args.fname,
            src_language=src_language,
            dest_language=dest_language,
            delay=args.delay,
            translator_name=args.translator,
            rename_source_file=args.rename,
            print_translation=args.print_translation,
            recursive=args.recursive
        )
    else:
        jupyter_translate(
            fname=args.fname,
            src_language=src_language,
            dest_language=dest_language,
            delay=args.delay,
            translator_name=args.translator,
            rename_source_file=args.rename,
            print_translation=args.print_translation
        )

if __name__ == '__main__':
    main()


