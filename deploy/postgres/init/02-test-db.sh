#!/bin/bash
# Create the test database and enable pgvector extension in it.
# This script runs inside the postgres container during initialization.

set -e

POSTGRES_DB="${POSTGRES_DB:-chatbot}"
POSTGRES_USER="${POSTGRES_USER:-chatbot}"

# Connect to the newly created main database as the postgres user
# (which is always available in the container startup context)
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    CREATE DATABASE chatbot_test;
EOSQL

# Enable search/vector extensions in the test database
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "chatbot_test" <<-EOSQL
    CREATE EXTENSION IF NOT EXISTS vector;
    CREATE EXTENSION IF NOT EXISTS pg_trgm;
EOSQL

echo "Test database 'chatbot_test' created with pgvector extension enabled."
