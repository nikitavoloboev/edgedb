#
# This source file is part of the EdgeDB open source project.
#
# Copyright 2008-present MagicStack Inc. and the EdgeDB authors.
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


from edb.lang.common import struct
from edb.lang.edgeql import ast as qlast

from . import abc as s_abc
from . import attributes
from . import delta as sd
from . import objects as so


class Database(attributes.AttributeSubject, s_abc.Database):

    # Override 'name' to str type, since databases don't have
    # fully-qualified names.
    name = so.SchemaField(str)


class DatabaseCommandContext(sd.CommandContextToken):
    pass


class DatabaseCommand(sd.ObjectCommand, schema_metaclass=Database,
                      context_class=DatabaseCommandContext):

    classname = struct.Field(str)

    @classmethod
    def _classname_from_ast(cls, schema, astnode, context):
        return astnode.name.name


class CreateDatabase(DatabaseCommand):
    astnode = qlast.CreateDatabase


class AlterDatabase(DatabaseCommand):
    astnode = qlast.AlterDatabase


class DropDatabase(DatabaseCommand):
    astnode = qlast.DropDatabase
