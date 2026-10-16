"""`ShellFlavour`: quoting, operators and script assembly for one shell."""

from __future__ import annotations

import abc
import collections.abc
import os
import typing
from pathlib import Path, PurePath

from ..executor import validate_environment_names, command_text, Environment, PathLike
from ._grammar import Cmdlet, Command, Exec, PSParam
from ._vocabulary import ShellCommand, ShellOperator, ShellTarget, ShellToken

#: Control characters that are ordinary syntax in raw shell source.
_RAW_WHITESPACE = frozenset(("\t", "\n", "\r"))


class ShellFlavour(abc.ABC):
    """Construct scripts and commands for one explicitly selected target shell."""

    name: str
    default_executable: str
    info_script: str
    command_separator: str
    line_terminator: str = "\n"
    #: Whether raw source may span lines (a heredoc, a block). Where it does,
    #: the separator after a command that contains a newline is a newline:
    #: `EOF;next` leaves the here-document open and swallows what follows.
    multiline_source: typing.ClassVar[bool] = False
    #: Appended to a rendered script before it is executed. Declared here so
    #: `script()` reads an attribute instead of duck-probing with `getattr`.
    execution_epilogue: str = ""
    context_order = ("env", "cwd", "command")
    #: How a failed `cd` is stopped from running the payload anyway.
    #:
    #: `"operator"` fuses the directory change onto a GROUPED payload with
    #: this flavour's AND operator, so the guard covers all of it: fused
    #: onto the payload directly, the AND bound to the first command only,
    #: and `cd /srv && a; b` ran `b` in the login directory and exited 0.
    #:
    #: `"statement"` is for a flavour whose change-directory statement
    #: aborts the script by itself -- PowerShell's `-ErrorAction Stop` --
    #: where there is nothing to fuse, and PowerShell 5 has no `&&` to fuse
    #: with anyway.
    #:
    #: Declared, not inferred from `context_order`: PowerShell's order puts
    #: `env` between `cwd` and `command`, and its `join_cwd` override still
    #: applies.
    cwd_guard: typing.ClassVar[str] = "operator"
    structured_command_prefix = ""
    path_flavor: type[PurePath] = PurePath

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.name!r}>"

    def escape_for_one_parse(self, text: str) -> str:
        """Escape `text` so ONE parse by this shell hands it back verbatim.

        This is the OUTER layer, for a transport that delivers a finalized
        command line to a login shell which parses it before the command
        line's own shell ever runs. SSH is the case: AsyncSSH sends one
        command string, and Windows OpenSSH's stock server hands it to
        `cmd.exe /c` -- so a line from `CmdShellFlavour.command()`, which
        already carries one cmd layer, is parsed by cmd twice.

        The default refuses rather than guessing. A POSIX login shell
        receiving a POSIX-rendered line is the ordinary single-parse case
        and never reaches here; a flavour that genuinely has an escape for
        an extra parse implements it.
        """
        raise NotImplementedError(
            f"{self.name} has no escaping for an additional parse by itself; "
            "the command line is already rendered for exactly one"
        )

    #: Whether `invocation()` can express this shell as argv.
    #:
    #: False for `cmd`: Windows builds a child's command line with
    #: `CreateProcess` quoting, which escapes every `"` an argv element
    #: carries, and cmd reads those escapes as literal data. A caller holding
    #: a flavour that says False must render with `command()` and submit the
    #: result as a command line (`executor.CommandLine`).
    argv_invocation: typing.ClassVar[bool] = True
    #: Whether `command()` accepts `stdin_free=True` and may then return a
    #: `ShellCommand` whose script travels on standard input.
    accepts_script_stdin: typing.ClassVar[bool] = False

    @staticmethod
    def _text(value: object) -> str:
        """Normalize values and reject shell control characters."""
        text = command_text(value, errors="surrogateescape")
        if any(ord(char) < 32 or ord(char) == 127 for char in text):
            raise ValueError("shell values cannot contain control characters")
        return text

    @staticmethod
    def _raw_source(value: str) -> str:
        """Validate raw shell *source*, which is not a value.

        A quoted value must not carry control characters -- that is what
        `_text` enforces. Raw text is the opposite case: it is shell source
        the caller wrote deliberately, where a newline is ordinary syntax
        (and where a generated script, such as PowerShell's multi-line exit
        epilogue, may legitimately be handed back for a second render). Only
        NUL and the other non-whitespace control characters are refused --
        nothing in any supported shell needs them and they defeat terminal
        and log inspection.
        """
        if any(
            (ord(char) < 32 and char not in _RAW_WHITESPACE) or ord(char) == 127
            for char in value
        ):
            raise ValueError("shell text cannot contain control characters")
        return value

    @abc.abstractmethod
    def quote(self, value: object) -> str:
        """Quote one value for THIS SHELL's parser.

        The syntactic layer only: enough that the shell sees one word and
        expands nothing. What the token meets after that is
        :meth:`argument`'s question.
        """

    def argument(
        self, value: object, *, target: ShellTarget = ShellTarget.NATIVE
    ) -> str:
        """Quote one value for `target`, the parser that reads it next.

        The default is every flavour whose shell hands tokens straight to
        `execve`: there is no second parser, so `quote()` is the whole
        answer. `cmd` and PowerShell override it, because on Windows the
        child re-parses its own command line and the two layers disagree.
        """
        del target
        return self.quote(value)

    def command_target(self, values: typing.Sequence[object]) -> ShellTarget:
        """Which parser this command's arguments will meet.

        Inferred where it is decidable -- `cmd` knows its own builtin list --
        and `NATIVE` otherwise. It is deliberately NOT inferred for
        PowerShell: whether `Remove-Item` is a cmdlet or an executable on the
        guest's PATH cannot be known without a runspace, and guessing is the
        class of mistake this package has been removing. A caller who knows
        passes `target=` to :meth:`argument` directly.
        """
        del values
        return ShellTarget.NATIVE

    @abc.abstractmethod
    def operator(self, value: ShellOperator) -> str:
        """Render one supported command operator."""

    def structured_command(self, values: typing.Iterable[object]) -> str:
        values = tuple(values)
        if any(isinstance(value, PSParam) for value in values):
            raise TypeError(
                f"PSParam is a PowerShell token; {self.name} has no use for it"
            )
        if not values:
            return self.structured_command_prefix
        target = self.command_target(values)
        # The first element names the program; it is never argv data.
        head = self.argument(values[0], target=ShellTarget.SHELL)
        return self.structured_command_prefix + " ".join(
            [head] + [self.argument(value, target=target) for value in values[1:]]
        )

    def cmdlet_command(self, value: Cmdlet) -> str:
        """Render a `Cmdlet`; only PowerShell has cmdlets."""
        raise TypeError(f"Cmdlet is a PowerShell command and cannot run in {self.name}")

    @abc.abstractmethod
    def environment_assignment(self, key: str, value: object) -> str:
        """Render one validated environment assignment."""

    def environment_script(self, env: Environment) -> str:
        """Convert an environment mapping into a standalone shell script."""
        assignments = []
        for key, value in env.items():
            if isinstance(key, bytes):
                key = key.decode("utf-8", "surrogateescape")
            validate_environment_names({key: value})
            assignments.append(self.environment_assignment(key, value))
        return self.command_separator.join(assignments)

    def command_text(self, value: Command) -> str:
        if isinstance(value, Exec):
            # Direct execution bypasses the shell entirely, so an `Exec` that
            # reaches here means a transport did not take its direct branch.
            # Rendering it as a quoted argv would silently reintroduce the
            # shell layer the caller asked to skip.
            raise TypeError(
                "Exec is executed directly and cannot be rendered into a shell "
                "script; this host does not support direct execution"
            )
        if isinstance(value, Cmdlet):
            return self.cmdlet_command(value)
        if isinstance(value, PSParam):
            raise TypeError("PSParam is only valid inside a PowerShell command")
        if isinstance(value, (bytes, PurePath, Path, os.PathLike)):
            # A standalone path is a program to run: PowerShell needs the call
            # operator or it prints the string, and cmd reads a command word.
            return self.structured_command((value,))
        if isinstance(value, str):
            return self._raw_source(value)
        if isinstance(value, collections.abc.Iterable):
            values = tuple(value)
            if not values:
                raise ValueError("structured command must not be empty")
            return self.structured_command(values)
        return self._text(value)

    def redirect_operand(self, value: Command) -> str:
        """Render the file name after `>` or `>>`.

        Raw text stays source. A path, bytes or a one-element structured
        command is a name, quoted for the shell's own parser: the redirect
        target is read by the shell, never by a child's C runtime.
        """
        if isinstance(value, str):
            return self._raw_source(value)
        if isinstance(value, (bytes, PurePath, Path, os.PathLike)):
            return self.argument(value, target=ShellTarget.SHELL)
        if isinstance(value, collections.abc.Iterable):
            values = tuple(value)
            if len(values) != 1:
                raise ValueError("a redirect takes exactly one file name")
            return self.argument(values[0], target=ShellTarget.SHELL)
        raise TypeError("a redirect operand must be a path or a one-element command")

    def redirect(self, value: ShellOperator, target: Command, operand: str) -> str:
        """Render a REDIRECT or APPEND `value` together with its target.

        `operand` is `redirect_operand(target)`; `target` is passed as well so
        a flavour can tell raw source from a file name.
        """
        del target
        return self.operator(value) + operand

    def join(self, values: typing.Iterable[ShellToken]) -> str:
        """Join commands, preserving raw strings and explicit operators."""
        result = []
        pending = self.command_separator
        redirecting: typing.Optional[ShellOperator] = None
        has_command = False
        expecting_command = False
        for value in values:
            if isinstance(value, ShellOperator):
                if not has_command or expecting_command:
                    raise ValueError("shell operator must appear between commands")
                pending = self.operator(value)
                redirecting = (
                    value
                    if value in (ShellOperator.REDIRECT, ShellOperator.APPEND)
                    else None
                )
                expecting_command = True
                continue
            if redirecting is not None:
                # What follows `>` is a file name, not a command.
                operand = self.redirect_operand(value)
                if not operand:
                    continue
                result.append(self.redirect(redirecting, value, operand))
            else:
                command = self.command_text(value)
                if not command:
                    continue
                if has_command:
                    if pending == self.command_separator:
                        pending = self._separator_after(result[-1])
                    result.append(pending)
                result.append(command)
            has_command = True
            expecting_command = False
            redirecting = None
            pending = self.command_separator
        if expecting_command:
            raise ValueError("shell operator must be followed by a command")
        return "".join(result)

    def script(
        self,
        cmds: typing.Iterable[ShellToken],
        *,
        cwd: typing.Optional[PathLike] = None,
        env: typing.Optional[Environment] = None,
        epilogue: bool = True,
    ) -> str:
        """Build a script, applying environment and cwd consistently.

        `epilogue=False` leaves off the flavour's status epilogue, for a
        script whose transport reports the status itself (`wants_epilogue`
        answers that for a set of capabilities) or that is one line of a
        persistent session.
        """
        command = self.join(cmds)
        changed = self.change_directory(cwd) if cwd is not None else ""
        rendered = {
            "env": self.environment_script(env) if env else "",
            "cwd": changed,
            "command": command,
        }
        if cwd is not None and command and self.cwd_guard == "operator":
            if "cwd" not in self.context_order or "command" not in self.context_order:
                raise ValueError(
                    f"{self.name}: cwd_guard='operator' needs both 'cwd' and "
                    f"'command' in context_order, found {self.context_order!r}"
                )
            rendered["cwd"] = self.join_cwd(changed, command)
            rendered["command"] = ""
        parts = [rendered[name] for name in self.context_order if rendered[name]]
        script = ""
        for part in parts:
            if script:
                script += self._separator_after(script)
            script += part
        if script and epilogue and self.execution_epilogue:
            script += self.execution_epilogue
        return script

    @abc.abstractmethod
    def change_directory(self, cwd: PathLike) -> str:
        """Render a directory change which fails if the directory is absent."""

    def _separator_after(self, command: str) -> str:
        """The separator that may follow `command` without breaking it."""
        if self.multiline_source and "\n" in command:
            return self.line_terminator
        return self.command_separator

    def group(self, command: str) -> str:
        """Wrap `command` so an operator applies to all of it, not its head."""
        if self.multiline_source and "\n" in command:
            # `; }` after a here-document delimiter would not close it.
            return "{ " + command + self.line_terminator + "}"
        return "{ " + command + "; }"

    def join_cwd(self, changed: str, command: str) -> str:
        """Join cwd setup and payload with the shell's AND operator.

        Reached only under `cwd_guard = "operator"`. The payload is grouped
        first: fused on directly, the AND bound to the *first* command only,
        so `cd /srv && a; b` ran `b` in the login directory and exited 0 --
        reachable through QGA (which always embeds cwd), fish or cmd over
        SSH, and any Shell over a capability-less executor.
        """
        return f"{changed}{self.operator(ShellOperator.AND)}{self.group(command)}"

    @abc.abstractmethod
    def command(
        self,
        cmds: typing.Iterable[ShellToken],
        *,
        executable: typing.Optional[str] = None,
        cwd: typing.Optional[PathLike] = None,
        env: typing.Optional[Environment] = None,
        login_shell: typing.Optional[str] = None,
    ) -> ShellCommand:
        """Build the command submitted to an SSH exec channel.

        The string is parsed first by `login_shell`: `None` means nothing
        parses it before the command line's own shell (as with
        `CreateProcess`), `"posix"` an sh-family shell or fish, `"cmd"` and
        `"powershell"` those shells. Each flavour's rule:

        * POSIX and fish join the invocation with `login_join`, which sh,
          bash, zsh, dash, ash and fish read back identically; `cwd` is part
          of the script. csh and tcsh are not supported.
        * PowerShell renders `-Command` for a `cmd` or POSIX login shell,
          the script on standard input (`ShellCommand.stdin`) when it
          cannot cross cmd.exe as an argument and the call leaves stdin
          free, and `-EncodedCommand <base64 utf-16le>` as the last resort.
        * `cmd` escapes exactly one cmd layer and, for `login_shell="cmd"`,
          one more for the login shell's own parse -- see the divergence
          ledger in `docs/guide/contracts.md`.
        """

    @abc.abstractmethod
    def invocation(
        self,
        script: str,
        *,
        executable: typing.Optional[str] = None,
    ) -> typing.Sequence[str]:
        """Build local-process argv which invokes this shell for one script.

        Raises `NotImplementedError` when this shell cannot be invoked that
        way -- see `argv_invocation`, and use `command()` instead.
        """

    def __str__(self) -> str:
        return self.name
