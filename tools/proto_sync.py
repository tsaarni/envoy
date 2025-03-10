#!/usr/bin/env python3

# Diff or copy protoxform artifacts from Bazel cache back to the source tree.

import os
import re
import shutil
import string
import subprocess
import sys

from api_proto_plugin import utils

from importlib.util import spec_from_loader, module_from_spec
from importlib.machinery import SourceFileLoader

# api/bazel/external_protos_deps.bzl must have a .bzl suffix for Starlark
# import, so we are forced to this workaround.
_external_proto_deps_spec = spec_from_loader(
    'external_proto_deps',
    SourceFileLoader('external_proto_deps', 'api/bazel/external_proto_deps.bzl'))
external_proto_deps = module_from_spec(_external_proto_deps_spec)
_external_proto_deps_spec.loader.exec_module(external_proto_deps)

# These .proto import direct path prefixes are already handled by
# api_proto_package() as implicit dependencies.
API_BUILD_SYSTEM_IMPORT_PREFIXES = [
    'google/api/annotations.proto',
    'google/protobuf/',
    'google/rpc/status.proto',
    'validate/validate.proto',
]

BUILD_FILE_TEMPLATE = string.Template(
    """# DO NOT EDIT. This file is generated by tools/proto_sync.py.

load("@envoy_api//bazel:api_build_system.bzl", "api_proto_package")

licenses(["notice"])  # Apache 2

api_proto_package($fields)
""")

IMPORT_REGEX = re.compile('import "(.*)";')
SERVICE_REGEX = re.compile('service \w+ {')


class ProtoSyncError(Exception):
  pass


class RequiresReformatError(ProtoSyncError):

  def __init__(self, message):
    super(RequiresReformatError, self).__init__(
        '%s; either run ./ci/do_ci.sh fix_format or ./tools/proto_format.sh fix to reformat.\n' %
        message)


def LabelPaths(label, src_suffix):
  """Compute single proto file source/destination paths from a Bazel proto label.

  Args:
    label: Bazel source proto label string.
    src_suffix: suffix string to append to source path.

  Returns:
    source, destination path tuple. The source indicates where in the Bazel
      cache the protoxform.py artifact with src_suffix can be found. The
      destination is a provisional path in the Envoy source tree for copying the
      contents of source when run in fix mode.
  """
  src = utils.BazelBinPathForOutputArtifact(label, src_suffix)
  dst = 'api/%s' % utils.ProtoFileCanonicalFromLabel(label)
  return src, dst


def SyncProtoFile(cmd, src, dst):
  """Diff or in-place update a single proto file from protoxform.py Bazel cache artifacts."

  Args:
    cmd: 'check' or 'fix'.
    src: source path.
    dst: destination path.
  """
  if cmd == 'fix':
    shutil.copyfile(src, dst)
  else:
    try:
      subprocess.check_call(['diff', src, dst])
    except subprocess.CalledProcessError:
      raise RequiresReformatError('%s and %s do not match' % (src, dst))


def SyncV2(cmd, src_labels):
  """Diff or in-place update v2 protos from protoxform.py Bazel cache artifacts."

  Args:
    cmd: 'check' or 'fix'.
    src_labels: Bazel label for source protos.
  """
  for s in src_labels:
    src, dst = LabelPaths(s, '.v2.proto')
    SyncProtoFile(cmd, src, dst)


def SyncV3Alpha(cmd, src_labels):
  """Diff or in-place update v3alpha protos from protoxform.py Bazel cache artifacts."

  Args:
    cmd: 'check' or 'fix'.
    src_labels: Bazel label for source protos.
  """
  for s in src_labels:
    src, dst = LabelPaths(s, '.v3alpha.proto')
    # Skip empty files, this indicates this file isn't modified in next version.
    if os.stat(src).st_size == 0:
      continue
    # Skip unversioned package namespaces. TODO(htuch): fix this to use the type
    # DB and proper upgrade paths.
    if 'v2' in dst:
      dst = re.sub('v2alpha\d?|v2', 'v3alpha', dst)
      SyncProtoFile(cmd, src, dst)
    elif 'envoy/type/matcher' in dst:
      dst = re.sub('/type/matcher/', '/type/matcher/v3alpha/', dst)
      SyncProtoFile(cmd, src, dst)
    elif 'envoy/type' in dst:
      dst = re.sub('/type/', '/type/v3alpha/', dst)
      SyncProtoFile(cmd, src, dst)


def GetImportDeps(proto_path):
  """Obtain the Bazel dependencies for the import paths from a .proto file.

  Args:
    proto_path: path to .proto.

  Returns:
    A list of Bazel targets reflecting the imports in the .proto at proto_path.
  """
  imports = []
  with open(proto_path, 'r', encoding='utf8') as f:
    for line in f:
      match = re.match(IMPORT_REGEX, line)
      if match:
        import_path = match.group(1)
        # We can ignore imports provided implicitly by api_proto_package().
        if any(import_path.startswith(p) for p in API_BUILD_SYSTEM_IMPORT_PREFIXES):
          continue
        # Special case handling for in-built versioning annotations.
        if import_path == 'udpa/api/annotations/versioning.proto':
          imports.append('@com_github_cncf_udpa//udpa/api/annotations:pkg')
          continue
        # Explicit remapping for external deps, compute paths for envoy/*.
        if import_path in external_proto_deps.EXTERNAL_PROTO_IMPORT_BAZEL_DEP_MAP:
          imports.append(external_proto_deps.EXTERNAL_PROTO_IMPORT_BAZEL_DEP_MAP[import_path])
          continue
        if import_path.startswith('envoy/'):
          # Ignore package internal imports.
          if os.path.dirname(os.path.join('api', import_path)) == os.path.dirname(proto_path):
            continue
          imports.append('//%s:pkg' % os.path.dirname(import_path))
          continue
        raise ProtoSyncError(
            'Unknown import path mapping for %s, please update the mappings in tools/proto_sync.py.\n'
            % import_path)
  return imports


def HasServices(proto_path):
  """Does a .proto file have any service definitions?

  Args:
    proto_path: path to .proto.

  Returns:
    True iff there are service definitions in the .proto at proto_path.
  """
  with open(proto_path, 'r', encoding='utf8') as f:
    for line in f:
      if re.match(SERVICE_REGEX, line):
        return True
  return False


def BuildFileContents(root, files):
  """Compute the canonical BUILD contents for an api/ proto directory.

  Args:
    root: base path to directory.
    files: a list of files in the directory.

  Returns:
    A string containing the canonical BUILD file content for root.
  """
  import_deps = set(sum([GetImportDeps(os.path.join(root, f)) for f in files], []))
  has_services = any(HasServices(os.path.join(root, f)) for f in files)
  fields = []
  if has_services:
    fields.append('    has_services = True,')
  if import_deps:
    if len(import_deps) == 1:
      formatted_deps = '"%s"' % list(import_deps)[0]
    else:
      formatted_deps = '\n' + '\n'.join(
          '        "%s",' % dep
          for dep in sorted(import_deps, key=lambda s: s.replace(':', '!'))) + '\n    '
    fields.append('    deps = [%s],' % formatted_deps)
  formatted_fields = '\n' + '\n'.join(fields) + '\n' if fields else ''
  return BUILD_FILE_TEMPLATE.substitute(fields=formatted_fields)


def SyncBuildFiles(cmd):
  """Diff or in-place update api/ BUILD files.

  Args:
    cmd: 'check' or 'fix'.
  """
  for root, dirs, files in os.walk('api/'):
    # Skip support files.
    if root == 'api/versioning':
      continue
    is_proto_dir = any(f.endswith('.proto') for f in files)
    if not is_proto_dir:
      continue
    build_contents = BuildFileContents(root, files)
    build_path = os.path.join(root, 'BUILD')
    if cmd == 'fix':
      with open(build_path, 'w') as f:
        f.write(build_contents)
    else:
      with open(build_path, 'r') as f:
        if build_contents != f.read():
          raise RequiresReformatError('%s is not canonically formatted' % build_path)


if __name__ == '__main__':
  cmd = sys.argv[1]
  src_labels = sys.argv[2:]
  try:
    SyncV2(cmd, src_labels)
    SyncV3Alpha(cmd, src_labels)
    SyncBuildFiles(cmd)
  except ProtoSyncError as e:
    sys.stderr.write('%s\n' % e)
    sys.exit(1)
