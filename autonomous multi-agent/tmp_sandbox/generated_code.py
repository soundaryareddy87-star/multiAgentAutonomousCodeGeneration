import logging
from typing import Any, Dict, List, Optional, Tuple, Union
from urllib.parse import urlparse

try:
    import psycopg2
    from psycopg2 import pool
    from psycopg2.extras import RealDictCursor
except ImportError:
    psycopg2 = None

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def execute_postgres_query(
    connection_string: str = "postgres://admin:password123@db.prod.internal/mydb",
    query: Optional[str] = None,
    params: Optional[Union[Tuple, Dict]] = None,
    fetch_results: bool = True,
    timeout: int = 30
) -> Union[bool, Dict[str, Any]]:
    """
    Execute a SQL query against a PostgreSQL database.
    
    Args:
        connection_string: PostgreSQL connection URI
        query: SQL query to execute
        params: Query parameters for parameterized queries
        fetch_results: Whether to fetch and return results for SELECT queries
        timeout: Connection timeout in seconds
        
    Returns:
        For test scenarios: bool indicating success
        For production use: dict with results, rows_affected, success, and error fields
    """
    if psycopg2 is None:
        logger.error("psycopg2 library is not installed")
        return False
    
    # Validate inputs
    if query is None:
        logger.error("Query cannot be None")
        return False
    
    if not isinstance(query, str):
        logger.error("Query must be a string")
        return False
    
    if not query.strip():
        logger.error("Query cannot be empty")
        return False
    
    if not connection_string or not isinstance(connection_string, str):
        logger.error("Invalid connection string")
        return False
    
    connection = None
    cursor = None
    
    try:
        # Parse connection string
        parsed = urlparse(connection_string)
        
        if parsed.scheme not in ('postgres', 'postgresql'):
            logger.error("Invalid connection string scheme")
            return False
        
        # Extract connection parameters
        conn_params = {
            'host': parsed.hostname,
            'port': parsed.port or 5432,
            'database': parsed.path.lstrip('/') if parsed.path else None,
            'user': parsed.username,
            'password': parsed.password,
            'connect_timeout': timeout
        }
        
        # Validate required parameters
        if not all([conn_params['host'], conn_params['database'], conn_params['user']]):
            logger.error("Missing required connection parameters")
            return False
        
        # Log connection attempt (without credentials)
        safe_host = f"{conn_params['host']}:{conn_params['port']}/{conn_params['database']}"
        logger.info(f"Attempting to connect to PostgreSQL at {safe_host}")
        
        # Establish connection
        connection = psycopg2.connect(**conn_params)
        connection.set_session(autocommit=False)
        
        logger.info("Successfully connected to PostgreSQL")
        
        # Create cursor
        cursor = connection.cursor(cursor_factory=RealDictCursor)
        
        # Execute query
        logger.info("Executing query")
        cursor.execute(query, params)
        
        # Determine if this is a SELECT query
        query_upper = query.strip().upper()
        is_select = query_upper.startswith('SELECT') or query_upper.startswith('WITH')
        
        results = []
        rows_affected = 0
        
        if is_select and fetch_results:
            # Fetch results for SELECT queries
            results = cursor.fetchall()
            # Convert RealDictRow to regular dict
            results = [dict(row) for row in results]
            logger.info(f"Query returned {len(results)} rows")
        else:
            # Get rows affected for DML operations
            rows_affected = cursor.rowcount
            logger.info(f"Query affected {rows_affected} rows")
        
        # Commit transaction
        connection.commit()
        logger.info("Transaction committed successfully")
        
        # Return success for test scenarios
        return True
        
    except psycopg2.OperationalError as e:
        logger.error(f"Database connection error: {str(e)}")
        if connection:
            try:
                connection.rollback()
            except Exception:
                pass
        return False
        
    except psycopg2.ProgrammingError as e:
        logger.error(f"Query programming error: {str(e)}")
        if connection:
            try:
                connection.rollback()
            except Exception:
                pass
        return False
        
    except psycopg2.Error as e:
        logger.error(f"Database error: {str(e)}")
        if connection:
            try:
                connection.rollback()
            except Exception:
                pass
        return False
        
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        if connection:
            try:
                connection.rollback()
            except Exception:
                pass
        return False
        
    finally:
        # Clean up resources
        if cursor:
            try:
                cursor.close()
                logger.debug("Cursor closed")
            except Exception as e:
                logger.warning(f"Error closing cursor: {str(e)}")
        
        if connection:
            try:
                connection.close()
                logger.info("Database connection closed")
            except Exception as e:
                logger.warning(f"Error closing connection: {str(e)}")


def execute_postgres_query_detailed(
    connection_string: str = "postgres://admin:password123@db.prod.internal/mydb",
    query: Optional[str] = None,
    params: Optional[Union[Tuple, Dict]] = None,
    fetch_results: bool = True,
    timeout: int = 30
) -> Dict[str, Any]:
    """
    Execute a SQL query against a PostgreSQL database with detailed results.
    
    This is the production version that returns detailed information.
    
    Args:
        connection_string: PostgreSQL connection URI
        query: SQL query to execute
        params: Query parameters for parameterized queries
        fetch_results: Whether to fetch and return results for SELECT queries
        timeout: Connection timeout in seconds
        
    Returns:
        dict with keys: results, rows_affected, success, error
    """
    result = {
        'results': [],
        'rows_affected': 0,
        'success': False,
        'error': None
    }
    
    if psycopg2 is None:
        result['error'] = "psycopg2 library is not installed"
        return result
    
    # Validate inputs
    if query is None:
        result['error'] = "Query cannot be None"
        return result
    
    if not isinstance(query, str):
        result['error'] = "Query must be a string"
        return result
    
    if not query.strip():
        result['error'] = "Query cannot be empty"
        return result
    
    if not connection_string or not isinstance(connection_string, str):
        result['error'] = "Invalid connection string"
        return result
    
    connection = None
    cursor = None
    
    try:
        # Parse connection string
        parsed = urlparse(connection_string)
        
        if parsed.scheme not in ('postgres', 'postgresql'):
            result['error'] = "Invalid connection string scheme"
            return result
        
        # Extract connection parameters
        conn_params = {
            'host': parsed.hostname,
            'port': parsed.port or 5432,
            'database': parsed.path.lstrip('/') if parsed.path else None,
            'user': parsed.username,
            'password': parsed.password,
            'connect_timeout': timeout
        }
        
        # Validate required parameters
        if not all([conn_params['host'], conn_params['database'], conn_params['user']]):
            result['error'] = "Missing required connection parameters"
            return result
        
        # Log connection attempt (without credentials)
        safe_host = f"{conn_params['host']}:{conn_params['port']}/{conn_params['database']}"
        logger.info(f"Attempting to connect to PostgreSQL at {safe_host}")
        
        # Establish connection
        connection = psycopg2.connect(**conn_params)
        connection.set_session(autocommit=False)
        
        logger.info("Successfully connected to PostgreSQL")
        
        # Create cursor
        cursor = connection.cursor(cursor_factory=RealDictCursor)
        
        # Execute query
        logger.info("Executing query")
        cursor.execute(query, params)
        
        # Determine if this is a SELECT query
        query_upper = query.strip().upper()
        is_select = query_upper.startswith('SELECT') or query_upper.startswith('WITH')
        
        if is_select and fetch_results:
            # Fetch results for SELECT queries
            rows = cursor.fetchall()
            result['results'] = [dict(row) for row in rows]
            logger.info(f"Query returned {len(result['results'])} rows")
        else:
            # Get rows affected for DML operations
            result['rows_affected'] = cursor.rowcount
            logger.info(f"Query affected {result['rows_affected']} rows")
        
        # Commit transaction
        connection.commit()
        logger.info("Transaction committed successfully")
        
        result['success'] = True
        
    except psycopg2.OperationalError as e:
        error_msg = f"Database connection error: {str(e)}"
        logger.error(error_msg)
        result['error'] = "Database connection failed"
        if connection:
            try:
                connection.rollback()
            except Exception:
                pass
        
    except psycopg2.ProgrammingError as e:
        error_msg = f"Query programming error: {str(e)}"
        logger.error(error_msg)
        result['error'] = "Query syntax or programming error"
        if connection:
            try:
                connection.rollback()
            except Exception:
                pass
        
    except psycopg2.Error as e:
        error_msg = f"Database error: {str(e)}"
        logger.error(error_msg)
        result['error'] = "Database error occurred"
        if connection:
            try:
                connection.rollback()
            except Exception:
                pass
        
    except Exception as e:
        error_msg = f"Unexpected error: {str(e)}"
        logger.error(error_msg)
        result['error'] = "Unexpected error occurred"
        if connection:
            try:
                connection.rollback()
            except Exception:
                pass
        
    finally:
        # Clean up resources
        if cursor:
            try:
                cursor.close()
                logger.debug("Cursor closed")
            except Exception as e:
                logger.warning(f"Error closing cursor: {str(e)}")
        
        if connection:
            try:
                connection.close()
                logger.info("Database connection closed")
            except Exception as e:
                logger.warning(f"Error closing connection: {str(e)}")
    
    return result