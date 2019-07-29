"""Execute Ansible sanity tests."""
from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import abc
import glob
import json
import os
import re
import collections

import lib.types as t

from lib.util import (
    ApplicationError,
    SubprocessError,
    display,
    import_plugins,
    load_plugins,
    parse_to_list_of_dict,
    ABC,
    ANSIBLE_ROOT,
    is_binary_file,
    read_lines_without_comments,
)

from lib.util_common import (
    run_command,
)

from lib.ansible_util import (
    ansible_environment,
    check_pyyaml,
)

from lib.target import (
    walk_internal_targets,
    walk_sanity_targets,
    TestTarget,
)

from lib.executor import (
    get_changes_filter,
    AllTargetsSkipped,
    Delegate,
    install_command_requirements,
    SUPPORTED_PYTHON_VERSIONS,
)

from lib.config import (
    SanityConfig,
)

from lib.test import (
    TestSuccess,
    TestFailure,
    TestSkipped,
    TestMessage,
    calculate_best_confidence,
)

from lib.data import (
    data_context,
)

from lib.env import (
    get_ansible_version,
)

COMMAND = 'sanity'


def command_sanity(args):
    """
    :type args: SanityConfig
    """
    changes = get_changes_filter(args)
    require = args.require + changes
    targets = SanityTargets(args.include, args.exclude, require)

    if not targets.include:
        raise AllTargetsSkipped()

    if args.delegate:
        raise Delegate(require=changes, exclude=args.exclude)

    install_command_requirements(args)

    tests = sanity_get_tests()

    if args.test:
        tests = [target for target in tests if target.name in args.test]
    else:
        disabled = [target.name for target in tests if not target.enabled and not args.allow_disabled]
        tests = [target for target in tests if target.enabled or args.allow_disabled]

        if disabled:
            display.warning('Skipping tests disabled by default without --allow-disabled: %s' % ', '.join(sorted(disabled)))

    if args.skip_test:
        tests = [target for target in tests if target.name not in args.skip_test]

    total = 0
    failed = []

    for test in tests:
        if args.list_tests:
            display.info(test.name)
            continue

        if isinstance(test, SanityMultipleVersion):
            versions = SUPPORTED_PYTHON_VERSIONS
        else:
            versions = (None,)

        for version in versions:
            if args.python and version and version != args.python_version:
                continue

            check_pyyaml(args, version or args.python_version)

            display.info('Sanity check using %s%s' % (test.name, ' with Python %s' % version if version else ''))

            options = ''

            if isinstance(test, SanityCodeSmellTest):
                result = test.test(args, targets)
            elif isinstance(test, SanityMultipleVersion):
                result = test.test(args, targets, python_version=version)
                options = ' --python %s' % version
            elif isinstance(test, SanitySingleVersion):
                result = test.test(args, targets)
            else:
                raise Exception('Unsupported test type: %s' % type(test))

            result.write(args)

            total += 1

            if isinstance(result, SanityFailure):
                failed.append(result.test + options)

    if failed:
        message = 'The %d sanity test(s) listed below (out of %d) failed. See error output above for details.\n%s' % (
            len(failed), total, '\n'.join(failed))

        if args.failure_ok:
            display.error(message)
        else:
            raise ApplicationError(message)


def collect_code_smell_tests():
    """
    :rtype: tuple[SanityFunc]
    """
    skip_file = os.path.join(ANSIBLE_ROOT, 'test/sanity/code-smell/skip.txt')
    ansible_only_file = os.path.join(ANSIBLE_ROOT, 'test/sanity/code-smell/ansible-only.txt')

    skip_tests = read_lines_without_comments(skip_file, remove_blank_lines=True, optional=True)

    if not data_context().content.is_ansible:
        skip_tests += read_lines_without_comments(ansible_only_file, remove_blank_lines=True)

    paths = glob.glob(os.path.join(ANSIBLE_ROOT, 'test/sanity/code-smell/*'))
    paths = sorted(p for p in paths if os.access(p, os.X_OK) and os.path.isfile(p) and os.path.basename(p) not in skip_tests)

    tests = tuple(SanityCodeSmellTest(p) for p in paths)

    return tests


def sanity_get_tests():
    """
    :rtype: tuple[SanityFunc]
    """
    return SANITY_TESTS


class SanityIgnoreParser:
    """Parser for the consolidated sanity test ignore file."""
    NO_CODE = '_'

    def __init__(self, args):  # type: (SanityConfig) -> None
        if data_context().content.collection:
            ansible_version = '%s.%s' % tuple(get_ansible_version(args).split('.')[:2])

            ansible_label = 'Ansible %s' % ansible_version
            file_name = 'ignore-%s.txt' % ansible_version
        else:
            ansible_label = 'Ansible'
            file_name = 'ignore.txt'

        self.args = args
        self.relative_path = os.path.join('test/sanity', file_name)
        self.path = os.path.join(data_context().content.root, self.relative_path)
        self.ignores = collections.defaultdict(lambda: collections.defaultdict(dict))  # type: t.Dict[str, t.Dict[str, t.Dict[str, int]]]
        self.skips = collections.defaultdict(lambda: collections.defaultdict(int))  # type: t.Dict[str, t.Dict[str, int]]
        self.parse_errors = []  # type: t.List[t.Tuple[int, int, str]]
        self.file_not_found_errors = []  # type: t.List[t.Tuple[int, str]]

        lines = read_lines_without_comments(self.path, optional=True)
        paths = set(data_context().content.all_files())
        tests_by_name = {}  # type: t.Dict[str, SanityTest]
        versioned_test_names = set()  # type: t.Set[str]
        unversioned_test_names = {}  # type: t.Dict[str, str]

        display.info('Read %d sanity test ignore line(s) for %s from: %s' % (len(lines), ansible_label, self.relative_path), verbosity=1)

        for test in sanity_get_tests():
            if isinstance(test, SanityMultipleVersion):
                versioned_test_names.add(test.name)
                tests_by_name.update(dict(('%s-%s' % (test.name, python_version), test) for python_version in SUPPORTED_PYTHON_VERSIONS))
            else:
                unversioned_test_names.update(dict(('%s-%s' % (test.name, python_version), test.name) for python_version in SUPPORTED_PYTHON_VERSIONS))
                tests_by_name[test.name] = test

        for line_no, line in enumerate(lines, start=1):
            if not line:
                self.parse_errors.append((line_no, 1, "Line cannot be empty or contain only a comment"))
                continue

            parts = line.split(' ')
            path = parts[0]
            codes = parts[1:]

            if not path:
                self.parse_errors.append((line_no, 1, "Line cannot start with a space"))
                continue

            if path not in paths:
                self.file_not_found_errors.append((line_no, path))
                continue

            if not codes:
                self.parse_errors.append((line_no, len(path), "Error code required after path"))
                continue

            code = codes[0]

            if not code:
                self.parse_errors.append((line_no, len(path) + 1, "Error code after path cannot be empty"))
                continue

            if len(codes) > 1:
                self.parse_errors.append((line_no, len(path) + len(code) + 2, "Error code cannot contain spaces"))
                continue

            parts = code.split('!')
            code = parts[0]
            commands = parts[1:]

            parts = code.split(':')
            test_name = parts[0]
            error_codes = parts[1:]

            test = tests_by_name.get(test_name)

            if not test:
                unversioned_name = unversioned_test_names.get(test_name)

                if unversioned_name:
                    self.parse_errors.append((line_no, len(path) + len(unversioned_name) + 2, "Sanity test '%s' cannot use a Python version like '%s'" % (
                        unversioned_name, test_name)))
                elif test_name in versioned_test_names:
                    self.parse_errors.append((line_no, len(path) + len(test_name) + 1, "Sanity test '%s' requires a Python version like '%s-%s'" % (
                        test_name, test_name, args.python_version)))
                else:
                    self.parse_errors.append((line_no, len(path) + 2, "Sanity test '%s' does not exist" % test_name))

                continue

            if commands and error_codes:
                self.parse_errors.append((line_no, len(path) + len(test_name) + 2, "Error code cannot contain both '!' and ':' characters"))
                continue

            if commands:
                command = commands[0]

                if len(commands) > 1:
                    self.parse_errors.append((line_no, len(path) + len(test_name) + len(command) + 3, "Error code cannot contain multiple '!' characters"))
                    continue

                if command == 'skip':
                    if not test.can_skip:
                        self.parse_errors.append((line_no, len(path) + len(test_name) + 2, "Sanity test '%s' cannot be skipped" % test_name))
                        continue

                    existing_line_no = self.skips.get(test_name, {}).get(path)

                    if existing_line_no:
                        self.parse_errors.append((line_no, 1, "Duplicate '%s' skip for path '%s' first found on line %d" % (test_name, path, existing_line_no)))
                        continue

                    self.skips[test_name][path] = line_no
                    continue

                self.parse_errors.append((line_no, len(path) + len(test_name) + 2, "Command '!%s' not recognized" % command))
                continue

            if not test.can_ignore:
                self.parse_errors.append((line_no, len(path) + 1, "Sanity test '%s' cannot be ignored" % test_name))
                continue

            if test.error_code:
                if not error_codes:
                    self.parse_errors.append((line_no, len(path) + len(test_name) + 1, "Sanity test '%s' requires an error code" % test_name))
                    continue

                error_code = error_codes[0]

                if len(error_codes) > 1:
                    self.parse_errors.append((line_no, len(path) + len(test_name) + len(error_code) + 3, "Error code cannot contain multiple ':' characters"))
                    continue
            else:
                if error_codes:
                    self.parse_errors.append((line_no, len(path) + len(test_name) + 2, "Sanity test '%s' does not support error codes" % test_name))
                    continue

                error_code = self.NO_CODE

            existing = self.ignores.get(test_name, {}).get(path, {}).get(error_code)

            if existing:
                if test.error_code:
                    self.parse_errors.append((line_no, 1, "Duplicate '%s' ignore for error code '%s' for path '%s' first found on line %d" % (
                        test_name, error_code, path, existing)))
                else:
                    self.parse_errors.append((line_no, 1, "Duplicate '%s' ignore for path '%s' first found on line %d" % (
                        test_name, path, existing)))

                continue

            self.ignores[test_name][path][error_code] = line_no

    @staticmethod
    def load(args):  # type: (SanityConfig) -> SanityIgnoreParser
        """Return the current SanityIgnore instance, initializing it if needed."""
        try:
            return SanityIgnoreParser.instance
        except AttributeError:
            pass

        SanityIgnoreParser.instance = SanityIgnoreParser(args)
        return SanityIgnoreParser.instance


class SanityIgnoreProcessor:
    """Processor for sanity test ignores for a single run of one sanity test."""
    def __init__(self,
                 args,  # type: SanityConfig
                 name,  # type: str
                 code,  # type: t.Optional[str]
                 python_version,  # type: t.Optional[str]
                 ):  # type: (...) -> None
        if python_version:
            full_name = '%s-%s' % (name, python_version)
        else:
            full_name = name

        self.args = args
        self.code = code
        self.parser = SanityIgnoreParser.load(args)
        self.ignore_entries = self.parser.ignores.get(full_name, {})
        self.skip_entries = self.parser.skips.get(full_name, {})
        self.used_line_numbers = set()  # type: t.Set[int]

    def filter_skipped_paths(self, paths):  # type: (t.List[str]) -> t.List[str]
        """Return the given paths, with any skipped paths filtered out."""
        return sorted(set(paths) - set(self.skip_entries.keys()))

    def filter_skipped_targets(self, targets):  # type: (t.List[TestTarget]) -> t.List[TestTarget]
        """Return the given targets, with any skipped paths filtered out."""
        return sorted(target for target in targets if target.path not in self.skip_entries)

    def process_errors(self, errors, paths):  # type: (t.List[SanityMessage], t.List[str]) -> t.List[SanityMessage]
        """Return the given errors filtered for ignores and with any settings related errors included."""
        errors = self.filter_messages(errors)
        errors.extend(self.get_errors(paths))

        errors = sorted(set(errors))

        return errors

    def filter_messages(self, messages):  # type: (t.List[SanityMessage]) -> t.List[SanityMessage]
        """Return a filtered list of the given messages using the entries that have been loaded."""
        filtered = []

        for message in messages:
            path_entry = self.ignore_entries.get(message.path)

            if path_entry:
                code = message.code if self.code else SanityIgnoreParser.NO_CODE
                line_no = path_entry.get(code)

                if line_no:
                    self.used_line_numbers.add(line_no)
                    continue

            filtered.append(message)

        return filtered

    def get_errors(self, paths):  # type: (t.List[str]) -> t.List[SanityMessage]
        """Return error messages related to issues with the file."""
        messages = []

        # unused errors

        unused = []  # type: t.List[t.Tuple[int, str, str]]

        for path in paths:
            path_entry = self.ignore_entries.get(path)

            if not path_entry:
                continue

            unused.extend((line_no, path, code) for code, line_no in path_entry.items() if line_no not in self.used_line_numbers)

        messages.extend(SanityMessage(
            code=self.code,
            message="Ignoring '%s' on '%s' is unnecessary" % (code, path) if self.code else "Ignoring '%s' is unnecessary" % path,
            path=self.parser.relative_path,
            line=line,
            column=1,
            confidence=calculate_best_confidence(((self.parser.path, line), (path, 0)), self.args.metadata) if self.args.metadata.changes else None,
        ) for line, path, code in unused)

        return messages


class SanitySuccess(TestSuccess):
    """Sanity test success."""
    def __init__(self, test, python_version=None):
        """
        :type test: str
        :type python_version: str
        """
        super(SanitySuccess, self).__init__(COMMAND, test, python_version)


class SanitySkipped(TestSkipped):
    """Sanity test skipped."""
    def __init__(self, test, python_version=None):
        """
        :type test: str
        :type python_version: str
        """
        super(SanitySkipped, self).__init__(COMMAND, test, python_version)


class SanityFailure(TestFailure):
    """Sanity test failure."""
    def __init__(self, test, python_version=None, messages=None, summary=None):
        """
        :type test: str
        :type python_version: str
        :type messages: list[SanityMessage]
        :type summary: unicode
        """
        super(SanityFailure, self).__init__(COMMAND, test, python_version, messages, summary)


class SanityMessage(TestMessage):
    """Single sanity test message for one file."""


class SanityTargets:
    """Sanity test target information."""
    def __init__(self, include, exclude, require):
        """
        :type include: list[str]
        :type exclude: list[str]
        :type require: list[str]
        """
        self.all = not include
        self.targets = tuple(sorted(walk_sanity_targets()))
        self.include = walk_internal_targets(self.targets, include, exclude, require)


class SanityTest(ABC):
    """Sanity test base class."""
    __metaclass__ = abc.ABCMeta

    ansible_only = False

    def __init__(self, name):
        self.name = name
        self.enabled = True

    @property
    def error_code(self):  # type: () -> t.Optional[str]
        """Error code for ansible-test matching the format used by the underlying test program, or None if the program does not use error codes."""
        return None

    @property
    def can_ignore(self):  # type: () -> bool
        """True if the test supports ignore entries."""
        return True

    @property
    def can_skip(self):  # type: () -> bool
        """True if the test supports skip entries."""
        return True


class SanityCodeSmellTest(SanityTest):
    """Sanity test script."""
    UNSUPPORTED_PYTHON_VERSIONS = (
        '2.6',  # some tests use voluptuous, but the version we require does not support python 2.6
    )

    def __init__(self, path):
        name = os.path.splitext(os.path.basename(path))[0]
        config_path = os.path.splitext(path)[0] + '.json'

        super(SanityCodeSmellTest, self).__init__(name)

        self.path = path
        self.config_path = config_path if os.path.exists(config_path) else None
        self.config = None

        if self.config_path:
            with open(self.config_path, 'r') as config_fd:
                self.config = json.load(config_fd)

        if self.config:
            self.enabled = not self.config.get('disabled')

    def test(self, args, targets):
        """
        :type args: SanityConfig
        :type targets: SanityTargets
        :rtype: TestResult
        """
        if args.python_version in self.UNSUPPORTED_PYTHON_VERSIONS:
            display.warning('Skipping %s on unsupported Python version %s.' % (self.name, args.python_version))
            return SanitySkipped(self.name)

        if self.path.endswith('.py'):
            cmd = [args.python_executable, self.path]
        else:
            cmd = [self.path]

        env = ansible_environment(args, color=False)

        pattern = None
        data = None

        settings = self.load_processor(args)

        paths = []

        if self.config:
            output = self.config.get('output')
            extensions = self.config.get('extensions')
            prefixes = self.config.get('prefixes')
            files = self.config.get('files')
            always = self.config.get('always')
            text = self.config.get('text')
            ignore_changes = self.config.get('ignore_changes')

            if output == 'path-line-column-message':
                pattern = '^(?P<path>[^:]*):(?P<line>[0-9]+):(?P<column>[0-9]+): (?P<message>.*)$'
            elif output == 'path-message':
                pattern = '^(?P<path>[^:]*): (?P<message>.*)$'
            else:
                pattern = ApplicationError('Unsupported output type: %s' % output)

            if ignore_changes:
                paths = sorted(i.path for i in targets.targets)
                always = False
            else:
                paths = sorted(i.path for i in targets.include)

            if always:
                paths = []

            if text is not None:
                if text:
                    paths = [p for p in paths if not is_binary_file(p)]
                else:
                    paths = [p for p in paths if is_binary_file(p)]

            if extensions:
                paths = [p for p in paths if os.path.splitext(p)[1] in extensions or (p.startswith('bin/') and '.py' in extensions)]

            if prefixes:
                paths = [p for p in paths if any(p.startswith(pre) for pre in prefixes)]

            if files:
                paths = [p for p in paths if os.path.basename(p) in files]

            paths = settings.filter_skipped_paths(paths)

            if not paths and not always:
                return SanitySkipped(self.name)

            data = '\n'.join(paths)

            if data:
                display.info(data, verbosity=4)

        try:
            stdout, stderr = run_command(args, cmd, data=data, env=env, capture=True)
            status = 0
        except SubprocessError as ex:
            stdout = ex.stdout
            stderr = ex.stderr
            status = ex.status

        if stdout and not stderr:
            if pattern:
                matches = parse_to_list_of_dict(pattern, stdout)

                messages = [SanityMessage(
                    message=m['message'],
                    path=m['path'],
                    line=int(m.get('line', 0)),
                    column=int(m.get('column', 0)),
                ) for m in matches]

                messages = settings.process_errors(messages, paths)

                if not messages:
                    return SanitySuccess(self.name)

                return SanityFailure(self.name, messages=messages)

        if stderr or status:
            summary = u'%s' % SubprocessError(cmd=cmd, status=status, stderr=stderr, stdout=stdout)
            return SanityFailure(self.name, summary=summary)

        messages = settings.process_errors([], paths)

        if messages:
            return SanityFailure(self.name, messages=messages)

        return SanitySuccess(self.name)

    def load_processor(self, args):  # type: (SanityConfig) -> SanityIgnoreProcessor
        """Load the ignore processor for this sanity test."""
        return SanityIgnoreProcessor(args, self.name, self.error_code, None)


class SanityFunc(SanityTest):
    """Base class for sanity test plugins."""
    def __init__(self):
        name = self.__class__.__name__
        name = re.sub(r'Test$', '', name)  # drop Test suffix
        name = re.sub(r'(.)([A-Z][a-z]+)', r'\1-\2', name).lower()  # use dashes instead of capitalization

        super(SanityFunc, self).__init__(name)


class SanitySingleVersion(SanityFunc):
    """Base class for sanity test plugins which should run on a single python version."""
    @abc.abstractmethod
    def test(self, args, targets):
        """
        :type args: SanityConfig
        :type targets: SanityTargets
        :rtype: TestResult
        """

    def load_processor(self, args):  # type: (SanityConfig) -> SanityIgnoreProcessor
        """Load the ignore processor for this sanity test."""
        return SanityIgnoreProcessor(args, self.name, self.error_code, None)


class SanityMultipleVersion(SanityFunc):
    """Base class for sanity test plugins which should run on multiple python versions."""
    @abc.abstractmethod
    def test(self, args, targets, python_version):
        """
        :type args: SanityConfig
        :type targets: SanityTargets
        :type python_version: str
        :rtype: TestResult
        """

    def load_processor(self, args, python_version):  # type: (SanityConfig, str) -> SanityIgnoreProcessor
        """Load the ignore processor for this sanity test."""
        return SanityIgnoreProcessor(args, self.name, self.error_code, python_version)


SANITY_TESTS = (
)


def sanity_init():
    """Initialize full sanity test list (includes code-smell scripts determined at runtime)."""
    import_plugins('sanity')
    sanity_plugins = {}  # type: t.Dict[str, t.Type[SanityFunc]]
    load_plugins(SanityFunc, sanity_plugins)
    sanity_tests = tuple([plugin() for plugin in sanity_plugins.values() if data_context().content.is_ansible or not plugin.ansible_only])
    global SANITY_TESTS  # pylint: disable=locally-disabled, global-statement
    SANITY_TESTS = tuple(sorted(sanity_tests + collect_code_smell_tests(), key=lambda k: k.name))
