"""
Delta application system for state changes.
Deltas are SQL operations emitted by projectors.
"""
import sqlite3
from typing import List
import json
from typing import Any


class DeltaApplicator:
    """Applies deltas (SQL operations) to the database."""
    
    @staticmethod
    def apply(delta: dict[str, Any], db: sqlite3.Connection) -> None:
        """
        Apply a delta to the database.
        
        Delta format:
        {
            "op": "insert|update|delete",
            "table": "table_name",
            "data": {...},  # for insert/update
            "where": {...}, # for update/delete
            "sql": "raw sql", # alternative: raw SQL
            "params": []      # params for raw SQL
        }
        """
        op = delta.get('op')
        
        if 'sql' in delta:
            # Raw SQL delta
            db.execute(delta['sql'], delta.get('params', []))
            return
        
        table = delta['table']
        
        if op == 'insert':
            columns = list(delta['data'].keys())
            values = list(delta['data'].values())
            placeholders = ','.join(['?' for _ in columns])
            column_list = ','.join(columns)
            
            sql = f"INSERT OR IGNORE INTO {table} ({column_list}) VALUES ({placeholders})"
            db.execute(sql, values)
            
        elif op == 'update':
            set_clause = ','.join([f"{k} = ?" for k in delta['data'].keys()])
            set_values = list(delta['data'].values())
            
            where_clause = ' AND '.join([f"{k} = ?" for k in delta.get('where', {}).keys()])
            where_values = list(delta.get('where', {}).values())
            
            sql = f"UPDATE {table} SET {set_clause}"
            if where_clause:
                sql += f" WHERE {where_clause}"
            
            db.execute(sql, set_values + where_values)
            
        elif op == 'delete':
            where_clause = ' AND '.join([f"{k} = ?" for k in delta.get('where', {}).keys()])
            where_values = list(delta.get('where', {}).values())
            
            sql = f"DELETE FROM {table}"
            if where_clause:
                sql += f" WHERE {where_clause}"
            
            db.execute(sql, where_values)
            
        elif op == 'upsert':
            # Insert or replace using provided key and data
            # Merge key and data dicts (data wins on conflicts)
            key = delta.get('key', {})
            data = delta.get('data', {})
            merged = dict(key)
            merged.update(data)
            columns = list(merged.keys())
            values = list(merged.values())
            placeholders = ','.join(['?' for _ in columns])
            column_list = ','.join(columns)
            sql = f"INSERT OR REPLACE INTO {table} ({column_list}) VALUES ({placeholders})"
            db.execute(sql, values)
        else:
            raise ValueError(f"Unknown delta operation: {op}")
    
    @staticmethod
    def apply_batch(deltas: List[dict[str, Any]], db: sqlite3.Connection) -> None:
        """Apply multiple deltas in a transaction."""
        try:
            for delta in deltas:
                DeltaApplicator.apply(delta, db)
            db.commit()
        except Exception:
            db.rollback()
            raise
