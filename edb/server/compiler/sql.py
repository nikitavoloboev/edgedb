#
# This source file is part of the EdgeDB open source project.
#
# Copyright 2016-present MagicStack Inc. and the EdgeDB authors.
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
#


from __future__ import annotations
from typing import Mapping, Sequence, List, TYPE_CHECKING, Optional

import dataclasses
import functools
import hashlib
import immutables
import json

from edb import errors
from edb.common import uuidgen
from edb.server import defines

from edb.schema import schema as s_schema

from edb.pgsql import ast as pgast
from edb.pgsql import common as pg_common
from edb.pgsql import codegen as pg_codegen
from edb.pgsql import params as pg_params
from edb.pgsql import parser as pg_parser

from . import dbstate
from . import enums

if TYPE_CHECKING:
    from edb.pgsql import resolver as pg_resolver


# Frontend-only settings. Maps setting name into their mutability flag.
FE_SETTINGS_MUTABLE: immutables.Map[str, bool] = immutables.Map(
    {
        'search_path': True,
        'allow_user_specified_id': True,
        'apply_access_policies_pg': True,
        'server_version': False,
        'server_version_num': False,
    }
)


def compile_sql(
    source: pg_parser.Source,
    *,
    schema: s_schema.Schema,
    tx_state: dbstate.SQLTransactionState,
    prepared_stmt_map: Mapping[str, str],
    current_database: str,
    current_user: str,
    allow_user_specified_id: Optional[bool],
    apply_access_policies: Optional[bool],
    include_edgeql_io_format_alternative: bool = False,
    allow_prepared_statements: bool = True,
    disambiguate_column_names: bool,
    backend_runtime_params: pg_params.BackendRuntimeParams,
    protocol_version: defines.ProtocolVersion,
) -> List[dbstate.SQLQueryUnit]:
    def _try(q: str) -> List[dbstate.SQLQueryUnit]:
        return _compile_sql(
            q,
            schema=schema,
            tx_state=tx_state,
            prepared_stmt_map=prepared_stmt_map,
            current_database=current_database,
            current_user=current_user,
            allow_user_specified_id=allow_user_specified_id,
            apply_access_policies=apply_access_policies,
            include_edgeql_io_format_alternative=(
                include_edgeql_io_format_alternative),
            allow_prepared_statements=allow_prepared_statements,
            disambiguate_column_names=disambiguate_column_names,
            backend_runtime_params=backend_runtime_params,
            protocol_version=protocol_version,
        )

    try:
        return _try(source.text())
    except errors.EdgeDBError as original_err:
        if isinstance(source, pg_parser.NormalizedSource):
            # try non-normalized source
            try:
                _try(source.original_text())
            except errors.EdgeDBError as denormalized_err:
                raise denormalized_err
            except Exception:
                raise original_err
            else:
                raise AssertionError(
                    "Normalized query is broken while original is valid")
        else:
            raise original_err


def _compile_sql(
    query_str: str,
    *,
    schema: s_schema.Schema,
    tx_state: dbstate.SQLTransactionState,
    prepared_stmt_map: Mapping[str, str],
    current_database: str,
    current_user: str,
    allow_user_specified_id: Optional[bool],
    apply_access_policies: Optional[bool],
    include_edgeql_io_format_alternative: bool = False,
    allow_prepared_statements: bool = True,
    disambiguate_column_names: bool,
    backend_runtime_params: pg_params.BackendRuntimeParams,
    protocol_version: defines.ProtocolVersion,
) -> List[dbstate.SQLQueryUnit]:
    opts = ResolverOptionsPartial(
        query_str=query_str,
        current_database=current_database,
        current_user=current_user,
        allow_user_specified_id=allow_user_specified_id,
        apply_access_policies=apply_access_policies,
        include_edgeql_io_format_alternative=(
            include_edgeql_io_format_alternative
        ),
        disambiguate_column_names=disambiguate_column_names,
    )

    stmts = pg_parser.parse(query_str, propagate_spans=True)
    sql_units = []
    for stmt in stmts:
        orig_text = pg_codegen.generate_source(stmt)
        fe_settings = tx_state.current_fe_settings()
        track_stats = False

        unit = dbstate.SQLQueryUnit(
            orig_query=orig_text,
            fe_settings=fe_settings,
            # by default, the query is sent to PostgreSQL unchanged
            query=orig_text,
        )

        if isinstance(stmt, (pgast.VariableSetStmt, pgast.VariableResetStmt)):
            if protocol_version != defines.POSTGRES_PROTOCOL:
                from edb.pgsql import resolver as pg_resolver
                pg_resolver.dispatch._raise_unsupported(stmt)

            value: Optional[dbstate.SQLSetting]
            if isinstance(stmt, pgast.VariableSetStmt):
                value = pg_arg_list_to_python(stmt.args)
            else:
                value = None

            fe_only = stmt.name and (
                # GOTCHA: setting is frontend-only regardless of its mutability
                stmt.name in FE_SETTINGS_MUTABLE
                or stmt.name.startswith('global ')
            )

            if fe_only:
                assert stmt.name
                if not FE_SETTINGS_MUTABLE.get(stmt.name, True):
                    raise errors.QueryError(
                        f'parameter "{stmt.name}" cannot be changed',
                        pgext_code='55P02',  # cant_change_runtime_param
                    )

                unit.set_vars = {stmt.name: value}
                unit.frontend_only = True
                unit.command_complete_tag = dbstate.TagPlain(
                    tag=(
                        b"SET"
                        if isinstance(stmt, pgast.VariableSetStmt)
                        else b"RESET"
                    )
                )
            elif stmt.scope == pgast.OptionsScope.SESSION:
                unit.set_vars = {stmt.name: value}

            unit.is_local = stmt.scope == pgast.OptionsScope.TRANSACTION
            if not unit.is_local:
                unit.capabilities |= enums.Capability.SESSION_CONFIG

        elif isinstance(stmt, pgast.VariableShowStmt):
            if protocol_version != defines.POSTGRES_PROTOCOL:
                from edb.pgsql import resolver as pg_resolver
                pg_resolver.dispatch._raise_unsupported(stmt)

            unit.get_var = stmt.name
            unit.frontend_only = (
                stmt.name in FE_SETTINGS_MUTABLE
                or stmt.name.startswith('global ')
            )
            if unit.frontend_only:
                unit.command_complete_tag = dbstate.TagPlain(tag=b"SHOW")

        elif isinstance(stmt, pgast.SetTransactionStmt):
            if protocol_version != defines.POSTGRES_PROTOCOL:
                from edb.pgsql import resolver as pg_resolver
                pg_resolver.dispatch._raise_unsupported(stmt)

            if stmt.scope == pgast.OptionsScope.SESSION:
                unit.set_vars = {
                    f"default_{name}": (
                        (
                            value.val
                            if isinstance(value, pgast.StringConstant)
                            else pg_codegen.generate_source(value)
                        ),
                    )
                    for name, value in stmt.options.options.items()
                }

        elif isinstance(stmt, (pgast.BeginStmt, pgast.StartStmt)):
            unit.tx_action = dbstate.TxAction.START
            unit.command_complete_tag = dbstate.TagPlain(
                tag=b"START TRANSACTION"
            )
        elif isinstance(stmt, pgast.CommitStmt):
            unit.tx_action = dbstate.TxAction.COMMIT
            unit.tx_chain = stmt.chain or False
            unit.command_complete_tag = dbstate.TagPlain(tag=b"COMMIT")

        elif isinstance(stmt, pgast.RollbackStmt):
            unit.tx_action = dbstate.TxAction.ROLLBACK
            unit.tx_chain = stmt.chain or False
            unit.command_complete_tag = dbstate.TagPlain(tag=b"ROLLBACK")

        elif isinstance(stmt, pgast.SavepointStmt):
            unit.tx_action = dbstate.TxAction.DECLARE_SAVEPOINT
            unit.sp_name = stmt.savepoint_name
            unit.command_complete_tag = dbstate.TagPlain(tag=b"SAVEPOINT")

        elif isinstance(stmt, pgast.ReleaseStmt):
            unit.tx_action = dbstate.TxAction.RELEASE_SAVEPOINT
            unit.sp_name = stmt.savepoint_name
            unit.command_complete_tag = dbstate.TagPlain(tag=b"RELEASE")

        elif isinstance(stmt, pgast.RollbackToStmt):
            unit.tx_action = dbstate.TxAction.ROLLBACK_TO_SAVEPOINT
            unit.sp_name = stmt.savepoint_name
            unit.command_complete_tag = dbstate.TagPlain(tag=b"ROLLBACK")

        elif isinstance(stmt, pgast.TwoPhaseTransactionStmt):
            raise NotImplementedError(
                "two-phase transactions are not supported"
            )
        elif isinstance(stmt, pgast.PrepareStmt):
            if not allow_prepared_statements:
                raise errors.UnsupportedFeatureError(
                    "SQL prepared statements are not supported"
                )

            # Translate the underlying query.
            stmt_resolved, stmt_source, _ = resolve_query(
                stmt.query, schema, tx_state, opts
            )
            if stmt.argtypes:
                param_types = []
                for pt in stmt.argtypes:
                    param_types.append(pg_codegen.generate_source(pt))
                param_text = f"({', '.join(param_types)})"
            else:
                param_text = ""

            sql_trailer = f"{param_text} AS ({stmt_source.text})"

            mangled_stmt_name = compute_stmt_name(
                f"PREPARE {pg_common.quote_ident(stmt.name)}{sql_trailer}",
                tx_state,
            )

            sql_text = (
                f"PREPARE {pg_common.quote_ident(mangled_stmt_name)}"
                f"{sql_trailer}"
            )

            unit.query = sql_text
            unit.prepare = dbstate.PrepareData(
                stmt_name=stmt.name,
                be_stmt_name=mangled_stmt_name.encode("utf-8"),
                query=stmt_source.text,
                translation_data=stmt_source.translation_data,
            )
            unit.command_complete_tag = dbstate.TagPlain(tag=b"PREPARE")
            track_stats = True

        elif isinstance(stmt, pgast.ExecuteStmt):
            if not allow_prepared_statements:
                raise errors.UnsupportedFeatureError(
                    "SQL prepared statements are not supported"
                )

            orig_name = stmt.name
            mangled_name = prepared_stmt_map.get(orig_name)
            if not mangled_name:
                raise errors.QueryError(
                    f"prepared statement \"{orig_name}\" does " f"not exist",
                    pgext_code='26000',  # invalid_sql_statement_name
                )
            stmt.name = mangled_name

            unit.query = pg_codegen.generate_source(stmt)
            unit.execute = dbstate.ExecuteData(
                stmt_name=orig_name,
                be_stmt_name=mangled_name.encode("utf-8"),
            )
            unit.cardinality = enums.Cardinality.MANY
            track_stats = True

        elif isinstance(stmt, pgast.DeallocateStmt):
            if not allow_prepared_statements:
                raise errors.UnsupportedFeatureError(
                    "SQL prepared statements are not supported"
                )
            orig_name = stmt.name
            mangled_name = prepared_stmt_map.get(orig_name)
            if not mangled_name:
                raise errors.QueryError(
                    f"prepared statement \"{orig_name}\" does " f"not exist",
                    pgext_code='26000',  # invalid_sql_statement_name
                )
            stmt.name = mangled_name

            unit.query = pg_codegen.generate_source(stmt)
            unit.deallocate = dbstate.DeallocateData(
                stmt_name=orig_name,
                be_stmt_name=mangled_name.encode("utf-8"),
            )
            unit.command_complete_tag = dbstate.TagPlain(tag=b"DEALLOCATE")

        elif isinstance(stmt, pgast.LockStmt):
            if stmt.mode not in ('ACCESS SHARE', 'ROW SHARE', 'SHARE'):
                raise NotImplementedError("exclusive lock is not supported")
            # just ignore
            unit.query = "DO $$ BEGIN END $$;"
        elif isinstance(stmt, (pgast.Query, pgast.CopyStmt)):
            stmt_resolved, stmt_source, edgeql_fmt_src = resolve_query(
                stmt, schema, tx_state, opts
            )
            unit.query = stmt_source.text
            unit.translation_data = stmt_source.translation_data
            if edgeql_fmt_src is not None:
                unit.eql_format_query = edgeql_fmt_src.text
                unit.eql_format_translation_data = (
                    edgeql_fmt_src.translation_data
                )
            unit.command_complete_tag = stmt_resolved.command_complete_tag
            unit.params = stmt_resolved.params
            if isinstance(stmt, pgast.DMLQuery) and not stmt.returning_list:
                unit.cardinality = enums.Cardinality.NO_RESULT
            else:
                unit.cardinality = enums.Cardinality.MANY
            track_stats = True
        else:
            from edb.pgsql import resolver as pg_resolver
            pg_resolver.dispatch._raise_unsupported(stmt)

        unit.stmt_name = compute_stmt_name(unit.query, tx_state).encode("utf-8")

        if track_stats and backend_runtime_params.has_stat_statements:
            cconfig: dict[str, dbstate.SQLSetting] = {
                k: v for k, v in fe_settings.items()
                if k is not None and v is not None and k in FE_SETTINGS_MUTABLE
            }
            cconfig.pop('server_version', None)
            cconfig.pop('server_version_num', None)
            if allow_user_specified_id is not None:
                cconfig.setdefault(
                    'allow_user_specified_id',
                    ('true' if allow_user_specified_id else 'false',),
                )
            if apply_access_policies is not None:
                cconfig.setdefault(
                    'apply_access_policies',
                    ('true' if apply_access_policies else 'false',),
                )
            search_path = parse_search_path(cconfig.pop("search_path", ("",)))
            cconfig = dict(sorted((k, v) for k, v in cconfig.items()))
            extras = {
                'cc': cconfig,  # compilation_config
                'pv': protocol_version,  # protocol_version
                'dn': ', '.join(search_path),  # default_namespace
            }
            sql_info = {
                'query': orig_text,
                'type': defines.QueryType.SQL,
                'extras': json.dumps(extras),
            }
            id_hash = hashlib.blake2b(digest_size=16)
            id_hash.update(
                json.dumps(sql_info).encode(defines.EDGEDB_ENCODING)
            )
            sql_info['id'] = str(uuidgen.from_bytes(id_hash.digest()))
            prefix = ''.join([
                '-- ',
                json.dumps(sql_info),
                '\n',
            ])
            unit.prefix_len = len(prefix)
            unit.query = prefix + unit.query
            if unit.eql_format_query is not None:
                unit.eql_format_query = prefix + unit.eql_format_query

        if isinstance(stmt, pgast.DMLQuery):
            unit.capabilities |= enums.Capability.MODIFICATIONS

        if unit.tx_action is not None:
            unit.capabilities |= enums.Capability.TRANSACTION

        tx_state.apply(unit)
        sql_units.append(unit)

    if not sql_units:
        # Cluvio will try to execute an empty query
        sql_units.append(
            dbstate.SQLQueryUnit(
                orig_query='',
                query='',
                fe_settings=tx_state.current_fe_settings(),
            )
        )

    return sql_units


@dataclasses.dataclass(kw_only=True, eq=False, repr=False)
class ResolverOptionsPartial:
    current_user: str
    current_database: str
    query_str: str
    allow_user_specified_id: Optional[bool]
    apply_access_policies: Optional[bool]
    include_edgeql_io_format_alternative: Optional[bool]
    disambiguate_column_names: bool


def resolve_query(
    stmt: pgast.Base,
    schema: s_schema.Schema,
    tx_state: dbstate.SQLTransactionState,
    opts: ResolverOptionsPartial,
) -> tuple[
    pg_resolver.ResolvedSQL,
    pg_codegen.SQLSource,
    Optional[pg_codegen.SQLSource],
]:
    from edb.pgsql import resolver as pg_resolver

    search_path: Sequence[str] = ("public",)
    try:
        setting = tx_state.get("search_path")
    except KeyError:
        setting = None
    search_path = parse_search_path(setting)

    allow_user_specified_id = lookup_bool_setting(
        tx_state, 'allow_user_specified_id'
    )
    if allow_user_specified_id is None:
        allow_user_specified_id = opts.allow_user_specified_id
    if allow_user_specified_id is None:
        allow_user_specified_id = False

    apply_access_policies = lookup_bool_setting(
        tx_state, 'apply_access_policies_pg'
    )
    if apply_access_policies is None:
        apply_access_policies = opts.apply_access_policies
    if apply_access_policies is None:
        apply_access_policies = False

    options = pg_resolver.Options(
        current_user=opts.current_user,
        current_database=opts.current_database,
        current_query=opts.query_str,
        search_path=search_path,
        allow_user_specified_id=allow_user_specified_id,
        apply_access_policies=apply_access_policies,
        include_edgeql_io_format_alternative=(
            opts.include_edgeql_io_format_alternative
        ),
        disambiguate_column_names=opts.disambiguate_column_names,
    )
    resolved = pg_resolver.resolve(stmt, schema, options)
    source = pg_codegen.generate(resolved.ast, with_translation_data=True)
    if resolved.edgeql_output_format_ast is not None:
        edgeql_format_source = pg_codegen.generate(
            resolved.edgeql_output_format_ast,
            with_translation_data=True,
        )
    else:
        edgeql_format_source = None
    return resolved, source, edgeql_format_source


def lookup_bool_setting(
    tx_state: dbstate.SQLTransactionState, name: str
) -> Optional[bool]:
    try:
        setting = tx_state.get(name)
    except KeyError:
        setting = None
    if setting and setting[0]:
        return is_setting_truthy(setting[0])
    return None


def is_setting_truthy(val: str | int | float) -> bool:
    if isinstance(val, str):
        truthy = {'on', 'true', 'yes', '1'}
        return val.lower() in truthy
    elif isinstance(val, int):
        return bool(val)
    else:
        return False


def compute_stmt_name(text: str, tx_state: dbstate.SQLTransactionState) -> str:
    stmt_hash = hashlib.sha1(text.encode("utf-8"))
    for setting_name in sorted(FE_SETTINGS_MUTABLE):
        try:
            setting_value = tx_state.get(setting_name)
        except KeyError:
            pass
        else:
            stmt_hash.update(f"{setting_name}:{setting_value}".encode("utf-8"))
    return f"edb{stmt_hash.hexdigest()}"


@functools.cache
def parse_search_path(search_path_str: list[str | int | float]) -> list[str]:
    return [part for part in search_path_str if isinstance(part, str)]


def pg_arg_list_to_python(expr: pgast.ArgsList) -> dbstate.SQLSetting:
    return tuple(pg_const_to_python(a) for a in expr.args)


def pg_const_to_python(expr: pgast.BaseExpr) -> str | int | float:
    "Converts a pg const expression into a Python value"

    if isinstance(expr, pgast.StringConstant):
        return expr.val

    if isinstance(expr, pgast.NumericConstant):
        try:
            return int(expr.val)
        except ValueError:
            return float(expr.val)

    raise NotImplementedError()
