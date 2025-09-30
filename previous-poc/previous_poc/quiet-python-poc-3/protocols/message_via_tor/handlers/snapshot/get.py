def execute(params, db):
    """
    Get a snapshot of the current database state.
    Returns both structured data and SQL dump.
    """
    try:
        # Get structured snapshot
        structured = db.snapshot()
        
        # Remove schema to reduce clutter
        if 'schema' in structured:
            del structured['schema']
        
        # Convert data to idiomatic JSON format
        if 'data' in structured:
            idiomatic_data = {}
            for table_name, table_data in structured['data'].items():
                # Skip event_store table as it has its own display
                if table_name == 'event_store':
                    continue
                # Convert from {columns: [...], rows: [...]} to just an array of objects
                if 'rows' in table_data:
                    idiomatic_data[table_name] = table_data['rows']
                else:
                    idiomatic_data[table_name] = table_data
            structured = idiomatic_data
        
        # Get SQL dump
        sql_dump = db.snapshot_to_sql()
        
        return {
            "api_response": {
                "status": "success",
                "structured": structured,
                "sql_dump": sql_dump
            }
        }
    except Exception as e:
        return {
            "api_response": {
                "status": "error", 
                "error": str(e)
            }
        }