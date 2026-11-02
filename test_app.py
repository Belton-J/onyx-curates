import io
from fastapi.testclient import TestClient
from main import app, seed_default_categories
from database import get_db, Base, engine
import models

# Ensure tables are created and default categories seeded
Base.metadata.create_all(bind=engine)
seed_default_categories()

client = TestClient(app)

def test_full_flow():
    print("\n--- 1. Testing Default Categories ---")
    db = next(get_db())
    categories = db.query(models.Category).all()
    print(f"Categories found: {[c.name for c in categories]}")
    assert len(categories) > 0, "Default categories should be seeded."
    test_category = categories[0]

    print("\n--- 2. Testing Public Homepage GET / ---")
    res = client.get("/")
    assert res.status_code == 200, f"Expected 200, got {res.status_code}"
    assert "ONYX CURATES" in res.text
    print("GET / passed successfully.")

    print("\n--- 3. Testing Category Filter GET /category/{slug} ---")
    res = client.get(f"/category/{test_category.slug}")
    assert res.status_code == 200, f"Expected 200, got {res.status_code}"
    assert test_category.name in res.text
    print(f"GET /category/{test_category.slug} passed successfully.")

    print("\n--- 4. Testing Admin Dashboard GET /admin ---")
    res = client.get("/admin")
    assert res.status_code == 200, f"Expected 200, got {res.status_code}"
    assert "Curated Look Studio" in res.text
    print("GET /admin passed successfully.")

    print("\n--- 5. Testing Create Look POST /admin/create-post with File Upload ---")
    # Create a dummy image in memory
    fake_image_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15c4\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    files = {
        "pin_image": ("aesthetic_look.png", io.BytesIO(fake_image_bytes), "image/png")
    }
    data = {
        "title": "Minimalist Japandi Living Room with Ribbed Vase",
        "category_id": str(test_category.id)
    }
    res = client.post("/admin/create-post", data=data, files=files, follow_redirects=False)
    assert res.status_code in [302, 303], f"Expected redirect, got {res.status_code}"
    print(f"POST /admin/create-post redirected to: {res.headers.get('location')}")

    # Verify look was saved in database
    db = next(get_db())
    post = db.query(models.Post).filter(models.Post.title == "Minimalist Japandi Living Room with Ribbed Vase").first()
    assert post is not None, "Post should exist in database."
    assert post.pin_image_url.startswith("/static/uploads/")
    print(f"Look created successfully: ID={post.id}, ImageURL={post.pin_image_url}")

    print("\n--- 6. Testing Add Product to Look POST /admin/post/{id}/add-product ---")
    product_data = {
        "asin": "B08N5WRWNW",
        "hook_text": "The exact matte black ribbed ceramic vase resting on the oak coffee table"
    }
    res = client.post(f"/admin/post/{post.id}/add-product", data=product_data, follow_redirects=False)
    assert res.status_code in [302, 303], f"Expected redirect, got {res.status_code}"
    print(f"POST /admin/post/{post.id}/add-product redirected to: {res.headers.get('location')}")

    # Verify product was attached
    db = next(get_db())
    prod = db.query(models.Product).filter(models.Product.post_id == post.id).first()
    assert prod is not None, "Product should exist in database."
    assert prod.asin == "B08N5WRWNW"
    assert "https://m.media-amazon.com" in prod.amazon_image_url
    assert "tag=onyxcurates-20" in prod.affiliate_url
    print(f"Product attached successfully: {prod.product_title} (${prod.price})")
    print(f"Affiliate URL: {prod.affiliate_url}")
    print(f"Amazon CDN Image: {prod.amazon_image_url}")

    print("\n--- 7. Testing 1:1 Pinterest Landing Page GET /post/{id} ---")
    res = client.get(f"/post/{post.id}")
    assert res.status_code == 200, f"Expected 200, got {res.status_code}"
    assert post.title in res.text
    assert prod.product_title in res.text
    assert prod.hook_text in res.text
    assert "Shop on Amazon" in res.text
    assert "As an Amazon Associate, Onyx Curates earns from qualifying purchases." in res.text
    print("GET /post/{id} 1:1 conversion page passed with Amazon compliance notice!")

    print("\n==========================================")
    print("ALL 7 TEST SUITES PASSED FLAWLESSLY!")
    print("==========================================")

if __name__ == "__main__":
    test_full_flow()
