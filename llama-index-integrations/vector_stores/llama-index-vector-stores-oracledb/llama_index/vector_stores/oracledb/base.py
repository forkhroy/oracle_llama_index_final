# OopCompanion:suppressRename
from __future__ import annotations

import array
import functools
import json
import logging
import math
import os
import traceback
import uuid
from enum import Enum
from typing import (
    Any,
    Dict,
    List,
    Optional,
    Type,
    cast,
    TypeVar,
    Callable,
    TYPE_CHECKING,
)

from llama_index.core.schema import (
    BaseNode,
    MetadataMode,
    NodeRelationship,
    RelatedNodeInfo,
    TextNode,
)
from llama_index.core.utils import iter_batch
from llama_index.core.vector_stores.types import (
    VectorStore,
    VectorStoreQuery,
    VectorStoreQueryResult,
)

if TYPE_CHECKING:
    from oracledb import Connection, DatabaseError


logger = logging.getLogger(__name__)
log_level = os.getenv("LOG_LEVEL", "ERROR").upper()
logging.basicConfig(
    level=getattr(logging, log_level),
    format="%(asctime)s - %(levelname)s - %(message)s",
)


class DistanceStrategy(Enum):
    COSINE = 1
    DOT_PRODUCT = 2
    EUCLIDEAN_DISTANCE = 3
    MANHATTAN_DISTANCE = 4
    HAMMING_DISTANCE = 5
    EUCLIDEAN_SQUARED = 6


# Define a type variable that can be any kind of function
T = TypeVar("T", bound=Callable[..., Any])


def _handle_exceptions(func: T) -> T:
    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except RuntimeError as db_err:
            # Handle a known type of error (e.g., DB-related) specifically
            logger.exception("DB-related error occurred.")
            raise RuntimeError(f"Failed due to a DB issue: {db_err}") from db_err
        except ValueError as val_err:
            # Handle another known type of error specifically
            logger.exception("Validation error.")
            raise ValueError(f"Validation failed: {val_err}") from val_err
        except Exception as e:
            # Generic handler for all other exceptions
            logger.exception(f"An unexpected error occurred: {e}")
            raise RuntimeError(f"Unexpected error: {e}") from e

    return cast(T, wrapper)


def _escape_str(value: str) -> str:
    BS = "\\"
    must_escape = (BS, "'")
    return (
        "".join(f"{BS}{c}" if c in must_escape else c for c in value) if value else ""
    )


column_config: Dict = {
    "id": {"type": "VARCHAR2(64) PRIMARY KEY", "extract_func": lambda x: x.node_id},
    "doc_id": {"type": "VARCHAR2(64)", "extract_func": lambda x: x.ref_doc_id},
    "text": {
        "type": "CLOB",
        "extract_func": lambda x: _escape_str(
            x.get_content(metadata_mode=MetadataMode.NONE) or ""
        ),
    },
    "node_info": {
        # Now specifying the column as CLOB intended for JSON, with a check constraint
        "type": "JSON",
        "extract_func": lambda x: json.dumps(x.node_info),
    },
    "metadata": {
        # Also specified as CLOB intended for JSON, with a check constraint
        "type": "JSON",
        "extract_func": lambda x: json.dumps(x.metadata),
    },
    "embedding": {
        "type": "VECTOR",
        "extract_func": lambda x: _stringify_list(x.get_embedding()),
    },
}


def _stringify_list(lst: List) -> str:
    return "[" + ",".join(str(item) for item in lst) + "]"


@_handle_exceptions
def _table_exists(client: Connection, table_name: str):
    try:
        with client.cursor() as cursor:
            cursor.execute(f"SELECT COUNT(*) FROM {table_name}")
            return True
    except DatabaseError as ex:
        err_obj = ex.args
        if err_obj[0].code == 942:
            return False
        raise


@_handle_exceptions
def _index_exists(client: Connection, index_name: str) -> bool:
    # Check if the index exists
    query = (
        "SELECT index_name FROM all_indexes WHERE upper(index_name) = upper(:idx_name)"
    )

    with client.cursor() as cursor:
        # Execute the query
        cursor.execute(query, idx_name=index_name.upper())
        result = cursor.fetchone()

    # Check if the index exists
    return result is not None


def _get_distance_function(distance_strategy: DistanceStrategy) -> str:
    # Dictionary to map distance strategies to their corresponding function names
    distance_strategy2function = {
        DistanceStrategy.EUCLIDEAN_DISTANCE: "EUCLIDEAN",
        DistanceStrategy.DOT_PRODUCT: "DOT",
        DistanceStrategy.COSINE: "COSINE",
        DistanceStrategy.MANHATTAN_DISTANCE: "MANHATTAN",
        DistanceStrategy.HAMMING_DISTANCE: "HAMMING",
        DistanceStrategy.EUCLIDEAN_SQUARED: "EUCLIDEAN_SQUARED",
    }

    # Attempt to return the corresponding distance function
    if distance_strategy in distance_strategy2function:
        return distance_strategy2function[distance_strategy]

    # If it's an unsupported distance strategy, raise an error
    raise ValueError(f"Unsupported distance strategy: {distance_strategy}")


def _get_index_name(base_name: str) -> str:
    unique_id = str(uuid.uuid4()).replace("-", "")
    return f"{base_name}_{unique_id}"


@_handle_exceptions
def _create_table(client: Connection, table_name: str) -> None:
    if not _table_exists(client, table_name):
        with client.cursor() as cursor:
            column_definitions = ", ".join(
                [f'{k} {v["type"]}' for k, v in column_config.items()]
            )

            # Generate the final DDL statement
            ddl = f"CREATE TABLE {table_name} (\n  {column_definitions}\n)"

            cursor.execute(ddl)
        logger.info("Table created successfully...")
    else:
        logger.info("Table already exists...")


@_handle_exceptions
def create_index(
    client: Connection,
    vector_store: OraLlamaVS,
    params: Optional[dict[str, Any]] = None,
) -> None:
    if params:
        if params["idx_type"] == "HNSW":
            _create_hnsw_index(
                client, vector_store.table_name, vector_store.distance_strategy, params
            )
        elif params["idx_type"] == "IVF":
            _create_ivf_index(
                client, vector_store.table_name, vector_store.distance_strategy, params
            )
        else:
            _create_hnsw_index(
                client, vector_store.table_name, vector_store.distance_strategy, params
            )


@_handle_exceptions
def _create_config(defaults: dict, params: dict) -> dict:
    config: dict = {}
    if params:
        config = params.copy()
        # Ensure compulsory parts are included
        for compulsory_key in ["idx_name", "parallel"]:
            if compulsory_key not in config:
                if compulsory_key == "idx_name":
                    config[compulsory_key] = _get_index_name(defaults[compulsory_key])
                else:
                    config[compulsory_key] = defaults[compulsory_key]

        # Validate keys in config against defaults
        for key in config:
            if key not in defaults:
                raise ValueError(f"Invalid parameter: {key}")
    else:
        config = defaults
    return config


@_handle_exceptions
def _create_hnsw_index(
    client: Connection,
    table_name: str,
    distance_strategy: DistanceStrategy,
    params: Optional[dict[str, Any]] = None,
) -> None:
    defaults = {
        "idx_name": "HNSW",
        "idx_type": "HNSW",
        "neighbors": 32,
        "efConstruction": 200,
        "accuracy": 90,
        "parallel": 8,
    }

    config = _create_config(defaults, params)

    # Base SQL statement
    idx_name = config["idx_name"]
    base_sql = f"create vector index {idx_name} on {table_name}(embedding) ORGANIZATION INMEMORY NEIGHBOR GRAPH"

    # Optional parts depending on parameters
    accuracy_part = " WITH TARGET ACCURACY {accuracy}" if "accuracy" in config else ""
    distance_part = f" DISTANCE {_get_distance_function(distance_strategy)}"

    parameters_part = ""
    if "neighbors" in config and "efConstruction" in config:
        parameters_part = " parameters (type {idx_type}, neighbors {neighbors}, efConstruction {efConstruction})"
    elif "neighbors" in config and "efConstruction" not in config:
        config["efConstruction"] = defaults["efConstruction"]
        parameters_part = " parameters (type {idx_type}, neighbors {neighbors}, efConstruction {efConstruction})"
    elif "neighbors" not in config and "efConstruction" in config:
        config["neighbors"] = defaults["neighbors"]
        parameters_part = " parameters (type {idx_type}, neighbors {neighbors}, efConstruction {efConstruction})"

    # Always included part for parallel
    parallel_part = " parallel {parallel}"

    # Combine all parts
    ddl_assembly = (
        base_sql + accuracy_part + distance_part + parameters_part + parallel_part
    )
    # Format the SQL with values from the params dictionary
    ddl = ddl_assembly.format(**config)

    # Check if the index exists
    if not _index_exists(client, config["idx_name"]):
        with client.cursor() as cursor:
            cursor.execute(ddl)
            logger.info("Index created successfully...")
    else:
        logger.info("Index already exists...")


@_handle_exceptions
def _create_ivf_index(
    client: Connection,
    table_name: str,
    distance_strategy: DistanceStrategy,
    params: Optional[dict[str, Any]] = None,
) -> None:
    # Default configuration
    defaults = {
        "idx_name": "IVF",
        "idx_type": "IVF",
        "neighbor_part": 32,
        "accuracy": 90,
        "parallel": 8,
    }

    config = _create_config(defaults, params)

    # Base SQL statement
    idx_name = config["idx_name"]
    base_sql = f"CREATE VECTOR INDEX {idx_name} ON {table_name}(embedding) ORGANIZATION NEIGHBOR PARTITIONS"

    # Optional parts depending on parameters
    accuracy_part = " WITH TARGET ACCURACY {accuracy}" if "accuracy" in config else ""
    distance_part = f" DISTANCE {_get_distance_function(distance_strategy)}"

    parameters_part = ""
    if "idx_type" in config and "neighbor_part" in config:
        parameters_part = f" PARAMETERS (type {config['idx_type']}, neighbor partitions {config['neighbor_part']})"

    # Always included part for parallel
    parallel_part = f" PARALLEL {config['parallel']}"

    # Combine all parts
    ddl_assembly = (
        base_sql + accuracy_part + distance_part + parameters_part + parallel_part
    )
    # Format the SQL with values from the params dictionary
    ddl = ddl_assembly.format(**config)

    # Check if the index exists
    if not _index_exists(client, config["idx_name"]):
        with client.cursor() as cursor:
            cursor.execute(ddl)
        logger.info("Index created successfully...")
    else:
        logger.info("Index already exists...")


@_handle_exceptions
def drop_table_purge(client: Connection, table_name: str):
    if _table_exists(client, table_name):
        cursor = client.cursor()
        with cursor:
            ddl = f"DROP TABLE {table_name} PURGE"
            cursor.execute(ddl)
        logger.info("Table dropped successfully...")
    else:
        logger.info("Table not found...")


@_handle_exceptions
def drop_index_if_exists(client: Connection, index_name: str):
    if _index_exists(client, index_name):
        drop_query = f"DROP INDEX {index_name}"
        with client.cursor() as cursor:
            cursor.execute(drop_query)
            logger.info(f"Index {index_name} has been dropped.")
    else:
        logger.exception(f"Index {index_name} does not exist.")


class OraLlamaVS(VectorStore):
    """`OraLlamaVS` vector store.

    To use, you should have both:
    - the ``oracledb`` python package installed
    - a connection string associated with a OracleVS having deployed an
       Search index

    Example:
        .. code-block:: python

            from llama-index.core.vectorstores import OracleVS
            from oracledb import oracledb

            with oracledb.connect(user = user, passwd = pwd, dsn = dsn) as connection:
                print ("Database version:", connection.version)
    """

    AMPLIFY_RATIO_LE5 = 100
    AMPLIFY_RATIO_GT5 = 20
    AMPLIFY_RATIO_GT50 = 10
    metadata_column: str = "metadata"

    def __init__(
        self,
        _client: Connection,
        table_name: str,
        distance_strategy: DistanceStrategy = DistanceStrategy.EUCLIDEAN_DISTANCE,
        batch_size: Optional[int] = 32,
        params: Optional[dict[str, Any]] = None,
    ):
        try:
            import oracledb  # noqa: F401
        except ImportError as e:
            raise ImportError(
                "Unable to import oracledb, please install with "
                "`pip install -U oracledb`."
            ) from e

        try:
            """Initialize with oracledb client."""
            self._client = _client
            """Initialize with necessary components."""

            self.table_name = table_name
            self.distance_strategy = distance_strategy
            self.batch_size = batch_size
            self.params = params

            _create_table(_client, self.table_name)

        except Exception as ex:
            print("An exception occurred ::", ex)
            traceback.print_exc()
            raise

    @property
    def client(self) -> Any:
        """Get client."""
        return self._client

    @classmethod
    def class_name(cls) -> str:
        return "OracleVS"

    def _append_meta_filter_condition(
        self, where_str: Optional[str], exact_match_filter: list
    ) -> str:
        filter_str = " AND ".join(
            f"JSON_VALUE({self.metadata_column}, '$.{filter_item.key}') = '{filter_item.value}'"
            for filter_item in exact_match_filter
        )
        if where_str is None:
            where_str = filter_str
        else:
            where_str += " AND " + filter_str
        return where_str

    def _build_insert(self, values: List[BaseNode]) -> (str, List[tuple]):
        _data = []
        for item in values:
            item_values = tuple(
                column["extract_func"](item) for column in column_config.values()
            )
            _data.append(item_values)

        dml = f"""
           INSERT INTO {self.table_name} ({", ".join(column_config.keys())})
           VALUES ({", ".join([':' + str(i + 1) for i in range(len(column_config))])})
        """
        return dml, _data

    def _build_query(
        self, distance_function: str, k: int, where_str: Optional[str] = None
    ):
        where_clause = f"WHERE {where_str}" if where_str else ""

        return f"""
            SELECT id,
                doc_id,
                text,
                node_info,
                metadata,
                vector_distance(embedding, :embedding, {distance_function}) AS distance
            FROM {self.table_name}
            {where_clause}
            ORDER BY distance
            FETCH APPROX FIRST {k} ROWS ONLY
        """

    def _build_hybrid_query(
        self, sub_query: str, query_str: str, similarity_top_k: int
    ) -> str:
        terms_pattern = [f"(?i){x}" for x in query_str.split(" ")]
        column_keys = column_config.keys()
        return (
            f"SELECT {','.join(filter(lambda k: k != 'embedding', column_keys))}, "
            f"distance FROM ({sub_query}) temp_table "
            f"ORDER BY length(multiMatchAllIndices(text, {terms_pattern})) "
            f"AS distance1 DESC, "
            f"log(1 + countMatches(text, '(?i)({query_str.replace(' ', '|')})')) "
            f"AS distance2 DESC limit {similarity_top_k}"
        )

    @_handle_exceptions
    def add(self, nodes: list[BaseNode], **kwargs: Any) -> list[str]:
        if not nodes:
            return []

        for result_batch in iter_batch(nodes, self.batch_size):
            dml, bind_values = self._build_insert(values=result_batch)

            with self._client.cursor() as cursor:
                # Use executemany to insert the batch
                cursor.executemany(dml, bind_values)
                self._client.commit()

        return [node.node_id for node in nodes]

    @_handle_exceptions
    def delete(self, doc_id: str, **kwargs: Any) -> None:
        with self._client.cursor() as cursor:
            ddl = f"DELETE FROM {self.table_name} WHERE id = :doc_id"
            cursor.execute(ddl, [doc_id])
            self._client.commit()

    @_handle_exceptions
    def get_clob_value(self, result: Any) -> str:
        clob_value = ""
        if result:
            if isinstance(result, LOB):
                clob_value = result.read()
            elif isinstance(result, str):
                clob_value = result
            else:
                raise Exception("Unexpected type:", type(result))
        return clob_value

    @_handle_exceptions
    def drop(self) -> None:
        drop_table_purge(self._client, self.table_name)

    @_handle_exceptions
    def query(self, query: VectorStoreQuery, **kwargs: Any) -> VectorStoreQueryResult:
        distance_function = _get_distance_function(self.distance_strategy)
        where_str = (
            f"doc_id in {_stringify_list(query.doc_ids)}" if query.doc_ids else None
        )

        if query.filters is not None:
            where_str = self._append_meta_filter_condition(
                where_str, query.filters.filters
            )

        # build query sql
        query_sql = self._build_query(
            distance_function, query.similarity_top_k, where_str
        )
        """
        if query.mode == VectorStoreQueryMode.HYBRID and query.query_str is not None:
            amplify_ratio = self.AMPLIFY_RATIO_LE5
            if 5 < query.similarity_top_k < 50:
                amplify_ratio = self.AMPLIFY_RATIO_GT5
            if query.similarity_top_k > 50:
                amplify_ratio = self.AMPLIFY_RATIO_GT50
            query_sql = self._build_hybrid_query(
                self._build_query(
                    query_embed=query.query_embedding,
                    k=query.similarity_top_k,
                    where_str=where_str,
                    limit=query.similarity_top_k * amplify_ratio,
                ),
                query.query_str,
                query.similarity_top_k,
            )
            logger.debug(f"hybrid query_statement={query_statement}")
        """
        embedding = array.array("f", query.query_embedding)
        with self._client.cursor() as cursor:
            cursor.execute(query_sql, embedding=embedding)
            results = cursor.fetchall()

            similarities = []
            ids = []
            nodes = []
            for result in results:
                doc_id = result[1]
                text = self.get_clob_value(result[2])
                node_info = (
                    json.loads(result[3]) if isinstance(result[3], str) else result[3]
                )
                metadata = (
                    json.loads(result[4]) if isinstance(result[4], str) else result[4]
                )

                if query.node_ids:
                    if result[0] not in query.node_ids:
                        continue

                if isinstance(node_info, dict):
                    start_char_idx = node_info.get("start", None)
                    end_char_idx = node_info.get("end", None)
                node = TextNode(
                    id_=result[0],
                    text=text,
                    metadata=metadata,
                    start_char=start_char_idx,
                    end_char=end_char_idx,
                    relationships={
                        NodeRelationship.SOURCE: RelatedNodeInfo(node_id=doc_id)
                    },
                )

                nodes.append(node)
                similarities.append(1.0 - math.exp(-result[5]))
                ids.append(result[0])
            return VectorStoreQueryResult(
                nodes=nodes, similarities=similarities, ids=ids
            )

    @classmethod
    @_handle_exceptions
    def from_documents(
        cls: Type[OraLlamaVS],
        docs: List[TextNode],
        table_name: str = "llama_index",
        **kwargs: Any,
    ) -> OraLlamaVS:
        """Return VectorStore initialized from texts and embeddings."""
        _client = kwargs.get("client")
        if _client is None:
            raise ValueError("client parameter is required...")
        params = kwargs.get("params")
        distance_strategy = kwargs.get("distance_strategy")
        drop_table_purge(_client, table_name)

        vss = cls(
            _client=_client,
            table_name=table_name,
            params=params,
            distance_strategy=distance_strategy,
        )
        vss.add(nodes=docs)
        return vss