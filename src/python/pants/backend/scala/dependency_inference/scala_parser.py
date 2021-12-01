# Copyright 2021 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).
from __future__ import annotations

import json
import logging
import os
import pkgutil
from dataclasses import dataclass
from typing import Any, Iterator, Mapping

from pants.core.util_rules.source_files import SourceFiles
from pants.engine.fs import (
    AddPrefix,
    CreateDigest,
    Digest,
    DigestContents,
    Directory,
    FileContent,
    MergeDigests,
    RemovePrefix,
)
from pants.engine.internals.selectors import Get, MultiGet
from pants.engine.process import (
    BashBinary,
    FallibleProcessResult,
    Process,
    ProcessExecutionFailure,
    ProcessResult,
)
from pants.engine.rules import collect_rules, rule
from pants.jvm.compile import ClasspathEntry
from pants.jvm.jdk_rules import JdkSetup
from pants.jvm.resolve.coursier_fetch import (
    ArtifactRequirements,
    Coordinate,
    MaterializedClasspath,
    MaterializedClasspathRequest,
)
from pants.option.global_options import GlobalOptions
from pants.util.frozendict import FrozenDict
from pants.util.logging import LogLevel
from pants.util.ordered_set import FrozenOrderedSet

logger = logging.getLogger(__name__)

PARSER_SCALA_VERSION = "2.13.7"
SCALAMETA_VERSION = "4.4.30"
CIRCE_VERSION = "0.14.1"

PARSER_SCALA_VERSION_MAJOR_MINOR = ".".join(PARSER_SCALA_VERSION.split(".")[0:2])

SCALAMETA_DEPENDENCIES = [
    Coordinate.from_coord_str(s)
    for s in [
        "org.scalameta:scalameta_2.13:4.4.30",
        "org.scala-lang:scala-library:2.13.7",
        "com.thesamet.scalapb:scalapb-runtime_2.13:0.11.4",
        "org.scalameta:parsers_2.13:4.4.30",
        "org.scala-lang:scala-compiler:2.13.7",
        "net.java.dev.jna:jna:5.8.0",
        "org.scalameta:trees_2.13:4.4.30",
        "org.scalameta:common_2.13:4.4.30",
        "com.lihaoyi:sourcecode_2.13:0.2.7",
        "org.jline:jline:3.20.0",
        "org.scalameta:fastparse-v2_2.13:2.3.1",
        "org.scala-lang.modules:scala-collection-compat_2.13:2.4.4",
        "org.scala-lang:scalap:2.13.7",
        "org.scala-lang:scala-reflect:2.13.7",
        "com.google.protobuf:protobuf-java:3.15.8",
        "com.thesamet.scalapb:lenses_2.13:0.11.4",
        "com.lihaoyi:geny_2.13:0.6.5",
    ]
]


CIRCE_DEPENDENCIES = [
    Coordinate.from_coord_str(s)
    for s in [
        "io.circe:circe-generic_2.13:0.14.1",
        "org.typelevel:simulacrum-scalafix-annotations_2.13:0.5.4",
        "org.typelevel:cats-core_2.13:2.6.1",
        "org.scala-lang:scala-library:2.13.6",
        "io.circe:circe-numbers_2.13:0.14.1",
        "com.chuusai:shapeless_2.13:2.3.7",
        "io.circe:circe-core_2.13:0.14.1",
        "org.typelevel:cats-kernel_2.13:2.6.1",
    ]
]

SCALA_PARSER_ARTIFACT_REQUIREMENTS = ArtifactRequirements(
    SCALAMETA_DEPENDENCIES + CIRCE_DEPENDENCIES
)


@dataclass(frozen=True)
class ScalaImport:
    name: str
    is_wildcard: bool

    @classmethod
    def from_json_dict(cls, data: Mapping[str, Any]):
        return cls(name=data["name"], is_wildcard=data["isWildcard"])

    def to_debug_json_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "is_wildcard": self.is_wildcard,
        }


@dataclass(frozen=True)
class ScalaSourceDependencyAnalysis:
    provided_symbols: FrozenOrderedSet[str]
    provided_symbols_encoded: FrozenOrderedSet[str]
    imports_by_scope: FrozenDict[str, tuple[ScalaImport, ...]]
    consumed_symbols_by_scope: FrozenDict[str, FrozenOrderedSet[str]]
    scopes: FrozenOrderedSet[str]

    def all_imports(self) -> Iterator[str]:
        # TODO: This might also be an import relative to its scope.
        for imports in self.imports_by_scope.values():
            for imp in imports:
                yield imp.name

    def fully_qualified_consumed_symbols(self) -> Iterator[str]:
        """Consumed symbols qualified in various ways.

        This method _will_ introduce false-positives, because we will assume that the symbol could
        have been provided by any wildcard import in scope, as well as being declared in the current
        package.
        """
        # Collect all wildcard imports.
        wildcard_imports_by_scope = {}
        for scope, imports in self.imports_by_scope.items():
            wildcard_imports = tuple(imp for imp in imports if imp.is_wildcard)
            if wildcard_imports:
                wildcard_imports_by_scope[scope] = wildcard_imports

        for scope, consumed_symbols in self.consumed_symbols_by_scope.items():
            parent_scope_wildcards = {
                wi
                for s, wildcard_imports in wildcard_imports_by_scope.items()
                for wi in wildcard_imports
                if scope.startswith(s)
            }
            for symbol in consumed_symbols:
                for scope in self.scopes:
                    yield f"{scope}.{symbol}"
                if not self.scopes or "." in symbol:
                    # TODO: Similar to #13545: we assume that a symbol containing a dot might already
                    # be fully qualified.
                    yield symbol
                for wildcard_scope in parent_scope_wildcards:
                    yield f"{wildcard_scope.name}.{symbol}"

    @classmethod
    def from_json_dict(cls, d: dict) -> ScalaSourceDependencyAnalysis:
        return cls(
            provided_symbols=FrozenOrderedSet(d["providedSymbols"]),
            provided_symbols_encoded=FrozenOrderedSet(d["providedSymbolsEncoded"]),
            imports_by_scope=FrozenDict(
                {
                    key: tuple(ScalaImport.from_json_dict(v) for v in values)
                    for key, values in d["importsByScope"].items()
                }
            ),
            consumed_symbols_by_scope=FrozenDict(
                {
                    key: FrozenOrderedSet(values)
                    for key, values in d["consumedSymbolsByScope"].items()
                }
            ),
            scopes=FrozenOrderedSet(d["scopes"]),
        )

    def to_debug_json_dict(self) -> dict[str, Any]:
        return {
            "provided_symbols": list(self.provided_symbols),
            "provided_symbols_encoded": list(self.provided_symbols_encoded),
            "imports_by_scope": {
                key: [v.to_debug_json_dict() for v in values]
                for key, values in self.imports_by_scope.items()
            },
            "consumed_symbols_by_scope": {
                k: sorted(list(v)) for k, v in self.consumed_symbols_by_scope.items()
            },
            "scopes": list(self.scopes),
        }


@dataclass(frozen=True)
class FallibleScalaSourceDependencyAnalysisResult:
    process_result: FallibleProcessResult


class ScalaParserCompiledClassfiles(ClasspathEntry):
    pass


@rule(level=LogLevel.DEBUG)
async def analyze_scala_source_dependencies(
    bash: BashBinary,
    jdk_setup: JdkSetup,
    processor_classfiles: ScalaParserCompiledClassfiles,
    source_files: SourceFiles,
) -> FallibleScalaSourceDependencyAnalysisResult:
    if len(source_files.files) > 1:
        raise ValueError(
            f"analyze_scala_source_dependencies expects sources with exactly 1 source file, but found {len(source_files.snapshot.files)}."
        )
    elif len(source_files.files) == 0:
        raise ValueError(
            "analyze_scala_source_dependencies expects sources with exactly 1 source file, but found none."
        )
    source_prefix = "__source_to_analyze"
    source_path = os.path.join(source_prefix, source_files.files[0])
    processorcp_relpath = "__processorcp"

    (
        tool_classpath,
        prefixed_processor_classfiles_digest,
        prefixed_source_files_digest,
    ) = await MultiGet(
        Get(
            MaterializedClasspath,
            MaterializedClasspathRequest(
                prefix="__toolcp",
                artifact_requirements=(SCALA_PARSER_ARTIFACT_REQUIREMENTS,),
            ),
        ),
        Get(Digest, AddPrefix(processor_classfiles.digest, processorcp_relpath)),
        Get(Digest, AddPrefix(source_files.snapshot.digest, source_prefix)),
    )

    tool_digest = await Get(
        Digest,
        MergeDigests(
            (
                prefixed_processor_classfiles_digest,
                tool_classpath.digest,
                jdk_setup.digest,
            )
        ),
    )
    merged_digest = await Get(
        Digest,
        MergeDigests(
            (
                tool_digest,
                prefixed_source_files_digest,
            )
        ),
    )

    analysis_output_path = "__source_analysis.json"

    process_result = await Get(
        FallibleProcessResult,
        Process(
            argv=[
                *jdk_setup.args(bash, [*tool_classpath.classpath_entries(), processorcp_relpath]),
                "org.pantsbuild.backend.scala.dependency_inference.ScalaParser",
                analysis_output_path,
                source_path,
            ],
            input_digest=merged_digest,
            output_files=(analysis_output_path,),
            use_nailgun=tool_digest,
            append_only_caches=jdk_setup.append_only_caches,
            env=jdk_setup.env,
            description=f"Analyzing {source_files.files[0]}",
            level=LogLevel.DEBUG,
        ),
    )

    return FallibleScalaSourceDependencyAnalysisResult(process_result=process_result)


@rule(level=LogLevel.DEBUG)
async def resolve_fallible_result_to_analysis(
    fallible_result: FallibleScalaSourceDependencyAnalysisResult,
    global_options: GlobalOptions,
) -> ScalaSourceDependencyAnalysis:
    # TODO(#12725): Just convert directly to a ProcessResult like this:
    # result = await Get(ProcessResult, FallibleProcessResult, fallible_result.process_result)
    if fallible_result.process_result.exit_code == 0:
        analysis_contents = await Get(
            DigestContents, Digest, fallible_result.process_result.output_digest
        )
        analysis = json.loads(analysis_contents[0].content)
        return ScalaSourceDependencyAnalysis.from_json_dict(analysis)
    raise ProcessExecutionFailure(
        fallible_result.process_result.exit_code,
        fallible_result.process_result.stdout,
        fallible_result.process_result.stderr,
        "Scala source dependency analysis failed.",
        local_cleanup=global_options.options.process_execution_local_cleanup,
    )


@rule
async def setup_scala_parser_classfiles(
    bash: BashBinary, jdk_setup: JdkSetup
) -> ScalaParserCompiledClassfiles:
    dest_dir = "classfiles"

    parser_source_content = pkgutil.get_data(
        "pants.backend.scala.dependency_inference", "ScalaParser.scala"
    )
    if not parser_source_content:
        raise AssertionError("Unable to find ScalaParser.scala resource.")

    parser_source = FileContent("ScalaParser.scala", parser_source_content)

    tool_classpath, parser_classpath, source_digest = await MultiGet(
        Get(
            MaterializedClasspath,
            MaterializedClasspathRequest(
                prefix="__toolcp",
                artifact_requirements=(
                    ArtifactRequirements(
                        [
                            Coordinate(
                                group="org.scala-lang",
                                artifact="scala-compiler",
                                version=PARSER_SCALA_VERSION,
                            ),
                            Coordinate(
                                group="org.scala-lang",
                                artifact="scala-library",
                                version=PARSER_SCALA_VERSION,
                            ),
                            Coordinate(
                                group="org.scala-lang",
                                artifact="scala-reflect",
                                version=PARSER_SCALA_VERSION,
                            ),
                        ]
                    ),
                ),
            ),
        ),
        Get(
            MaterializedClasspath,
            MaterializedClasspathRequest(
                prefix="__parsercp", artifact_requirements=(SCALA_PARSER_ARTIFACT_REQUIREMENTS,)
            ),
        ),
        Get(
            Digest,
            CreateDigest(
                [
                    parser_source,
                    Directory(dest_dir),
                ]
            ),
        ),
    )

    merged_digest = await Get(
        Digest,
        MergeDigests(
            (
                tool_classpath.digest,
                parser_classpath.digest,
                jdk_setup.digest,
                source_digest,
            )
        ),
    )

    # NB: We do not use nailgun for this process, since it is launched exactly once.
    process_result = await Get(
        ProcessResult,
        Process(
            argv=[
                *jdk_setup.args(bash, tool_classpath.classpath_entries()),
                "scala.tools.nsc.Main",
                "-bootclasspath",
                ":".join(tool_classpath.classpath_entries()),
                "-classpath",
                ":".join(parser_classpath.classpath_entries()),
                "-d",
                dest_dir,
                parser_source.path,
            ],
            input_digest=merged_digest,
            append_only_caches=jdk_setup.append_only_caches,
            env=jdk_setup.env,
            output_directories=(dest_dir,),
            description="Compile Scala parser for dependency inference with scalac",
            level=LogLevel.DEBUG,
        ),
    )
    stripped_classfiles_digest = await Get(
        Digest, RemovePrefix(process_result.output_digest, dest_dir)
    )
    return ScalaParserCompiledClassfiles(digest=stripped_classfiles_digest)


def rules():
    return collect_rules()