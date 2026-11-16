import os
from dotenv import load_dotenv
from supabase import create_client, Client

# Force Python to read the newest .env file
load_dotenv(override=True)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

print(f"Connecting to: {SUPABASE_URL}")
print(f"Using Key starting with: {SUPABASE_KEY[:15]}...")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

try:
    print("\nAttempting to upload test file to 'pin-images'...")
    # Upload a tiny text file directly from memory
    res = supabase.storage.from_("pin-images").upload(
        path="test-diagnostic.txt",
        file=b"Hello from the diagnostic test!",
        file_options={"content-type": "text/plain"}
    )
    print("✅ UPLOAD SUCCESSFUL!")
    print("Response:", res)
    
except Exception as e:
    print("\n❌ UPLOAD FAILED!")
    print(f"Exact Error: {str(e)}")