#!/usr/bin/env python3
# Copyright 2024 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Fixing fuzz target with LLM."""

import argparse
import logging
import os
import re
import sys
from typing import Callable, Optional

from experiment import benchmark as benchmarklib
from llm_toolkit import models
from llm_toolkit import output_parser as parser
from llm_toolkit import prompt_builder

ERROR_LINES = 20


def parse_args():
  """Parses command line arguments."""
  argparser = argparse.ArgumentParser(
      description='Fix the raw fuzz targets generated by LLM.')
  argparser.add_argument(
      '-t',
      '--target-dir',
      type=str,
      default='./fixed_targets',
      help='The directory to store all fixed LLM-generated targets.')
  argparser.add_argument(
      '-o',
      '--intermediate-output-dir',
      type=str,
      default='./code_fix_output',
      help=('The directory to store all intermediate output files (LLM prompt, '
            'rawoutput).'))
  argparser.add_argument('-p',
                         '--project',
                         type=str,
                         required=True,
                         help='The project name.')
  argparser.add_argument('-f',
                         '--function',
                         type=str,
                         required=True,
                         help='The function name.')
  argparser.add_argument('-l',
                         '--log',
                         type=str,
                         required=True,
                         help='The build log file containing the error to fix.')

  args = argparser.parse_args()
  if args.target_dir and os.listdir(args.target_dir):
    assert os.path.isdir(
        args.target_dir
    ), f'--target-dir must take an existing directory: {args.target_dir}.'
    assert os.listdir(
        args.target_dir
    ), f'--target-dir must take a non-empty directory: {args.target_dir}.'

  os.makedirs(args.intermediate_output_dir, exist_ok=True)

  return args


def get_target_files(target_dir: str) -> list[str]:
  """Returns the fuzz target files in the raw target directory."""
  return [
      os.path.join(target_dir, f)
      for f in os.listdir(target_dir)
      if benchmarklib.is_c_file(f) or benchmarklib.is_cpp_file(f)
  ]


def collect_specific_fixes(project: str,
                           file_name: str) -> list[Callable[[str], str]]:
  """Returns a list code fix functions given the language and |project|."""
  required_fixes = set()
  if benchmarklib.is_cpp_file(file_name):
    required_fixes = required_fixes.union([
        append_extern_c,
        insert_cstdint,
        insert_cstdlib,
    ])

  # TODO(Dongge): Remove this.
  if benchmarklib.is_c_file(file_name):
    required_fixes = required_fixes.union([
        insert_stdint,
        include_builtin_library,
    ])

  # TODO(Dongge): Remove this.
  if project == 'libpng-proto':
    required_fixes = required_fixes.union([
        remove_nonexist_png_functions,
        include_pngrio,
        remove_const_from_png_symbols,
    ])

  return list(required_fixes)


def apply_specific_fixes(content: str,
                         required_fixes: list[Callable[[str], str]]) -> str:
  """Fixes frequent errors in |raw_content| and returns fixed content."""
  for required_fix in required_fixes:
    content = required_fix(content)

  return content


def fix_all_targets(target_dir: str, project: str):
  """Reads raw content, applies fixes, and saves the fixed content."""
  for file in get_target_files(target_dir):
    with open(file) as raw_file:
      raw_content = raw_file.read()
    specific_fixes = collect_specific_fixes(project, file)
    fixed_content = apply_specific_fixes(raw_content, specific_fixes)
    with open(os.path.join(target_dir, os.path.basename(file)),
              'w+') as fixed_file:
      fixed_file.write(fixed_content)


# ========================= Specific Fixes ========================= #
def append_extern_c(raw_content: str) -> str:
  """Appends `extern "C"` before fuzzer entry `LLVMFuzzerTestOneInput`."""
  pattern = r'int LLVMFuzzerTestOneInput'
  replacement = f'extern "C" {pattern}'
  fixed_content = re.sub(pattern, replacement, raw_content)
  return fixed_content


def insert_cstdlib(raw_content: str) -> str:
  """Includes `cstdlib` library."""
  fixed_content = f'#include <cstdlib>\n{raw_content}'
  return fixed_content


def insert_cstdint(raw_content: str) -> str:
  """Includes `cstdint` library."""
  fixed_content = f'#include <cstdint>\n{raw_content}'
  return fixed_content


def insert_stdint(content: str) -> str:
  """Includes `stdint` library."""
  include_stdint = '#include <stdint.h>\n'
  if include_stdint not in content:
    content = f'{include_stdint}{content}'
  return content


def remove_nonexist_png_functions(content: str) -> str:
  """Removes non-exist functions in libpng-proto."""
  non_exist_functions = [
      r'.*png_init_io.*',
      r'.*png_set_write_fn.*',
      r'.*png_set_compression_level.*',
      r'.*.png_write_.*',
  ]
  for pattern in non_exist_functions:
    content = re.sub(pattern, '', content)
  return content


def include_builtin_library(content: str) -> str:
  """Includes builtin libraries when its function was invoked."""
  library_function_dict = {
      '#include <stdlib.h>': [
          'malloc',
          'calloc',
          'free',
      ],
      '#include <string.h>': ['memcpy',]
  }
  for library, functions in library_function_dict.items():
    use_lib_functions = any(f in content for f in functions)
    if use_lib_functions and not library in content:
      content = f'{library}\n{content}'
  return content


def include_pngrio(content: str) -> str:
  """Includes <pngrio.c> when using its functions."""
  functions = [
      'png_read_data',
      'png_default_read_data',
  ]
  use_pngrio_funcitons = any(f in content for f in functions)
  include_pngrio_stmt = '#include "pngrio.c"'

  if use_pngrio_funcitons and not include_pngrio_stmt in content:
    content = f'{include_pngrio}\n{content}'
  return content


def remove_const_from_png_symbols(content: str) -> str:
  """Removes const from png types."""
  re.sub(r'png_const_', 'png_', content)
  return content


# ========================= LLM Fixes ========================= #


def get_fuzz_target_compile_output(target_names: list[str],
                                   compile_args: list[str]) -> str:
  """
  Given a list of |target_names|, returns the output file name specified by `-o`
  if the |compile_args| contains one of the |target_names|, or empty string.
  """
  output_name = ''
  target_found = ''
  for i, arg in enumerate(compile_args):
    if arg in ['-o'] or arg == '--output' and i < len(compile_args):
      output_name = compile_args[i + 1]
      continue
    if arg.startswith('--output='):
      output_name = arg.replace('--output=', '', 1)
      continue
    if arg.startswith('-o'):
      output_name = arg.replace('-o', '', 1)
      continue
    if arg.startswith('-'):
      continue
    if arg == output_name:
      continue
    if os.path.basename(arg) in target_names:
      target_found = os.path.basename(arg)

  if target_found:
    # In case output name was not specified by -o.
    if not output_name:
      if '-c' in compile_args:
        target_name, _ = os.path.splitext(target_found)
        return f'{target_name}.o'
      logging.warning(
          'Output file not specified in [%s], but fuzz target found',
          ' '.join(compile_args))
      return 'a.out'
    return os.path.basename(output_name)
  return ''


def get_jcc_errstr(errlog_path: str, project_target_basename: str) -> list[str]:
  """Extracts error message directly from jcc's err.log"""
  with open(errlog_path) as errlog_file:
    log_lines = errlog_file.readlines()

  target_names = [
      project_target_basename,
      # Look for headers corrected source,
      # from jcc.go:GetHeaderCorrectedFilename().
      f'jcc-corrected-{project_target_basename}'
  ]

  # Look for cppified target, from jcc.go:TryFixCCompilation().
  target_name, target_ext = os.path.splitext(project_target_basename)
  if target_ext == '.c':
    target_names.append(f'{target_name}.cpp')
    target_names.append(f'jcc-corrected-{target_name}.cpp')

  command_pattern = r'\[.*clang(?:\+\+)? (.*)\]\n?'
  clang_error_pattern = r'clang.*: error: .*n?'

  error_lines_range: list[Optional[int]] = [None, None]
  temp_range: list[Optional[int]] = [None, None]
  errors = []

  for i, line in enumerate(log_lines):
    command_match = re.fullmatch(command_pattern, line)
    if command_match:
      if temp_range[0] is not None:
        temp_range[1] = i
        if temp_range[0] < temp_range[1]:
          first_line = log_lines[temp_range[0]].rstrip()
          if first_line and not re.fullmatch(clang_error_pattern, first_line):
            error_lines_range = temp_range
        temp_range = [None, None]

      compile_target = get_fuzz_target_compile_output(
          target_names,
          command_match.group(1).split())
      if temp_range[0] is None and compile_target:
        target_names.append(compile_target)
        temp_range[0] = i + 1

  # Start of error was never set, cannot find target error message.
  if error_lines_range[0] is None and temp_range[0] is None:
    logging.error('Cannot find error message from err.log: %s', errlog_path)
    return []

  # The last error block was target error message.
  if temp_range[0] is not None and temp_range[0] < len(log_lines):
    first_line = log_lines[temp_range[0]].rstrip()
    if first_line and not re.fullmatch(clang_error_pattern, first_line):
      error_lines_range = temp_range
      error_lines_range[1] = len(log_lines)

  for line in log_lines[error_lines_range[0]:error_lines_range[1]]:
    uncolored_line = re.sub(r'\x1B([@-Z\\-_]|\[[0-?]*[ -/]*[@-~])', '', line)
    errors.append(uncolored_line.rstrip())
  return group_error_messages(errors)


def extract_error_message(log_path: str,
                          project_target_basename: str) -> list[str]:
  """Extracts error message and its context from the file in |log_path|."""

  with open(log_path) as log_file:
    # A more accurate way to extract the error message.
    log_lines = log_file.readlines()

  target_name, _ = os.path.splitext(project_target_basename)

  error_lines_range: list[Optional[int]] = [None, None]
  temp_range: list[Optional[int]] = [None, None]

  clang_error_start_pattern = (r'\S*' + target_name +
                               r'(\.\S*)?:\d+:\d+: .+: .+\n?')
  clang_include_error_pattern = (r'In file included from \S*' + target_name +
                                 r'(\.\S*)?:\d+:\n?')
  clang_error_end_pattern = r'.*\d+ errors? generated.\n?'

  linker_error_start_string = '/usr/bin/ld: '
  llvm_linker_error_start_string = 'ld.lld: '
  linker_error_end_pattern = (
      r'clang.*: error: linker command failed with exit code 1 \(use -v to see '
      r'invocation\)\n?')

  errors = []
  for i, line in enumerate(log_lines):
    if temp_range[0] is None:
      if (line.startswith(linker_error_start_string) or
          line.startswith(llvm_linker_error_start_string) or
          re.fullmatch(clang_include_error_pattern, line) or
          re.fullmatch(clang_error_start_pattern, line)):
        temp_range[0] = i
        continue

    # In case the original fuzz target was written in C and building with
    # clang failed, and building with clang++ also failed, we take the
    # error from clang++, which comes after.
    if temp_range[0] is not None:
      if (re.fullmatch(linker_error_end_pattern, line) or
          re.fullmatch(clang_error_end_pattern, line)):
        temp_range[1] = i
        error_lines_range = temp_range
        temp_range = [None, None]

  if error_lines_range[0] is None:
    logging.warning('Failed to parse error message from %s.', log_path)
    return []

  for line in log_lines[error_lines_range[0]:error_lines_range[1]]:
    uncolored_line = re.sub(r'\x1B([@-Z\\-_]|\[[0-?]*[ -/]*[@-~])', '', line)
    errors.append(uncolored_line.rstrip())
  return group_error_messages(errors)


def group_error_messages(error_lines: list[str]) -> list[str]:
  """Groups multi-line error block into one string"""
  state_unknown = 'UNKNOWN'
  state_include = 'INCLUDE'
  state_diag = 'DIAG'
  state_ld = 'LD'

  diag_error_pattern = re.compile(r'(\S*):\d+:\d+: (.+): (.+)')
  include_error_pattern = re.compile(r'In file included from (\S*):\d+:')
  diag_error_end_pattern = r'.*\d+ errors? generated.'

  linker_error_string = '/usr/bin/ld: '
  llvm_linker_error_string = 'ld.lld: '
  linker_error_end_string = ('clang-15: error: linker command failed with exit '
                             'code 1 (use -v to see invocation)')
  dwarf_error_string = 'DWARF error: invalid or unhandled FORM value: '

  error_blocks = []
  curr_block = []
  src_file = ''
  curr_state = state_unknown
  for line in error_lines:
    if not line:  # Trim empty lines.
      continue

    # clang/clang++ diagnostic errors.
    diag_match = diag_error_pattern.fullmatch(line)
    if diag_match:
      err_src = diag_match.group(1)
      severity = diag_match.group(2)

      # Matched a note diag line under another diag,
      # giving help info to fix the previous error.
      if severity == 'note':
        curr_block.append(line)
        continue

      # Matched a diag line but under an included file line,
      # indicating the specific error in the included file,
      if curr_state == state_include and err_src != src_file:
        curr_block.append(line)
        continue

      curr_state = state_diag
      if curr_block:
        error_blocks.append('\n'.join(curr_block))
        curr_block = []

    include_match = include_error_pattern.fullmatch(line)
    if include_match:
      src_file = include_match.group(1)
      curr_state = state_include
      if curr_block:
        error_blocks.append('\n'.join(curr_block))
        curr_block = []

    if re.fullmatch(diag_error_end_pattern, line):
      curr_state = state_unknown
      error_blocks.append('\n'.join(curr_block))
      curr_block = []
      continue

    # Linker errors.
    if (line.startswith(linker_error_string) or
        line.startswith(llvm_linker_error_string)):
      curr_state = state_ld
      if curr_block:
        error_blocks.append('\n'.join(curr_block))
        curr_block = []
      # Skip DWARF error line
      if dwarf_error_string in line:
        continue
      line = line.split(' ', maxsplit=1)[1]

    if line.startswith(linker_error_end_string):
      curr_state = state_unknown
      error_blocks.append('\n'.join(curr_block))
      curr_block = []
      continue

    # Keep unknown error lines separated.
    if curr_state == state_unknown and curr_block:
      error_blocks.append('\n'.join(curr_block))
      curr_block = []

    curr_block.append(line)

  if curr_block:
    error_blocks.append('\n'.join(curr_block))
  return error_blocks


def llm_fix(ai_binary: str, target_path: str, benchmark: benchmarklib.Benchmark,
            llm_fix_id: int, error_desc: Optional[str], errors: list[str],
            fixer_model_name: str) -> None:
  """Reads and fixes |target_path| in place with LLM based on |error_log|."""
  fuzz_target_source_code = parser.parse_code(target_path)

  _, target_ext = os.path.splitext(os.path.basename(target_path))
  response_dir = f'{os.path.splitext(target_path)[0]}-F{llm_fix_id}'
  os.makedirs(response_dir, exist_ok=True)
  prompt_path = os.path.join(response_dir, 'prompt.txt')

  apply_llm_fix(ai_binary,
                benchmark,
                fuzz_target_source_code,
                error_desc,
                errors,
                prompt_path,
                response_dir,
                fixer_model_name,
                temperature=0.5 - llm_fix_id * 0.04)

  fixed_code_candidates = []
  for file in os.listdir(response_dir):
    if not parser.is_raw_output(file):
      continue
    fixed_code_path = os.path.join(response_dir, file)
    fixed_code = parser.parse_code(fixed_code_path)
    fixed_code_candidates.append([fixed_code_path, fixed_code])

  if not fixed_code_candidates:
    print(f'LLM did not generate rawoutput for {prompt_path}.')
    return

  # TODO(Dongge): Use the common vote:
  # LLM gives multiple responses to one query. In many experiments, I
  # found the code compartment of some of the responses are exactly the same. In
  # these cases, we can use the most common code of all responses as it could be
  # a safer choice. Currently, we prefer the longest code to encourage code
  # complexity.
  # TODO(Dongge): Exclude the candidate if it is identical to the original
  # code.
  preferred_fix_path, preferred_fix_code = max(fixed_code_candidates,
                                               key=lambda x: len(x[1]))
  print(f'Will use the longest fix: {os.path.relpath(preferred_fix_path)}.')
  preferred_fix_name, _ = os.path.splitext(preferred_fix_path)
  fixed_target_path = os.path.join(response_dir,
                                   f'{preferred_fix_name}{target_ext}')
  parser.save_output(preferred_fix_code, fixed_target_path)
  parser.save_output(preferred_fix_code, target_path)


def apply_llm_fix(ai_binary: str,
                  benchmark: benchmarklib.Benchmark,
                  fuzz_target_source_code: str,
                  error_desc: Optional[str],
                  errors: list[str],
                  prompt_path: str,
                  response_dir: str,
                  fixer_model_name: str = models.DefaultModel.name,
                  temperature: float = 0.4):
  """Queries LLM to fix the code."""
  fixer_model = models.LLM.setup(
      ai_binary=ai_binary,
      name=fixer_model_name,
      num_samples=1,
      temperature=temperature,
  )

  builder = prompt_builder.DefaultTemplateBuilder(fixer_model)
  prompt = builder.build_fixer_prompt(benchmark, fuzz_target_source_code,
                                      error_desc, errors)
  prompt.save(prompt_path)

  fixer_model.generate_code(prompt, response_dir)


def main():
  args = parse_args()
  fix_all_targets(args.target_dir, args.project)


if __name__ == '__main__':
  sys.exit(main())
