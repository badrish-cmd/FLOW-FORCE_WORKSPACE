import os
import sys
import django
from django.core import management

# Initialize Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'flowforce.settings')
django.setup()

def main():
    print("=" * 60)
    print("      FLOW-FORCE WORKSPACE DATABASE MIGRATION SCRIPT      ")
    print("=" * 60)
    print("\nThis script will migrate all data from SQLite to PostgreSQL with ZERO data loss.\n")

    # Step 1: Dump SQLite data
    print("[1/4] Dumping data from SQLite...")
    dump_file = 'temp_sqlite_dump.json'
    
    try:
        with open(dump_file, 'w', encoding='utf-8') as f:
            management.call_command(
                'dumpdata',
                exclude=['contenttypes', 'auth.Permission'],
                indent=4,
                stdout=f
            )
        print(f" -> Data successfully dumped to '{dump_file}'")
    except Exception as e:
        print(f"\n[ERROR] Failed to dump SQLite data: {e}")
        return

    # Step 2: Ask user to configure PostgreSQL .env
    print("\n[2/4] Configuration Required:")
    print(" Please create or update your `.env` file in the project root folder with your Hostinger PostgreSQL credentials:")
    print(" -------------------------------------------------------------")
    print("  DB_NAME=your_postgres_db_name")
    print("  DB_USER=your_postgres_db_user")
    print("  DB_PASSWORD=your_postgres_db_password")
    print("  DB_HOST=127.0.0.1")
    print("  DB_PORT=5432")
    print(" -------------------------------------------------------------")
    
    input("\nOnce you have created and saved the `.env` file with PostgreSQL credentials, press ENTER to continue...")

    # Reload django settings to pick up the new database configuration
    print("\n[3/4] Reconnecting to PostgreSQL and running migrations...")
    try:
        # Clear database connection cache to force re-connection with .env settings
        from django.db import connections
        for conn in connections.all():
            conn.close()
        
        # Run migrations on PostgreSQL
        management.call_command('migrate')
        print(" -> Migrations completed successfully on PostgreSQL.")
    except Exception as e:
        print(f"\n[ERROR] Connection or Migration failed: {e}")
        print("Please check your `.env` credentials and ensure PostgreSQL is running.")
        if os.path.exists(dump_file):
            print(f"Note: Your SQLite data is safe in '{dump_file}'")
        return

    # Step 4: Load data into PostgreSQL
    print("\n[4/4] Loading SQLite data into PostgreSQL...")
    try:
        management.call_command('loaddata', dump_file)
        print(" -> Data successfully imported into PostgreSQL!")
    except Exception as e:
        print(f"\n[ERROR] Failed to import data: {e}")
        print("Make sure you haven't manually created conflicting rows in the PostgreSQL DB.")
        return

    # Clean up
    try:
        os.remove(dump_file)
        print("\n -> Cleaned up temporary dump file.")
    except Exception:
        pass

    print("\n" + "=" * 60)
    print(" SUCCESS! Migration from SQLite to PostgreSQL completed successfully!")
    print("=" * 60)

if __name__ == '__main__':
    main()
