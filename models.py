from datetime import datetime
from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey, Text
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from database import Base


class Category(Base):
    __tablename__ = "categories"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    slug = Column(String(120), unique=True, index=True, nullable=False)

    # Relationships
    posts = relationship(
        "Post",
        back_populates="category",
        cascade="all, delete-orphan"
    )

    def __repr__(self):
        return f"<Category(id={self.id}, name='{self.name}', slug='{self.slug}')>"


class Post(Base):
    """
    Represents 'The Look' / Pin target page.
    Direct 1:1 destination for incoming Pinterest pins.
    """
    __tablename__ = "posts"

    id = Column(Integer, primary_key=True, index=True)
    category_id = Column(Integer, ForeignKey("categories.id", ondelete="CASCADE"), nullable=False)
    title = Column(String(255), nullable=False)
    pin_image_url = Column(String(500), nullable=False)  # Local static upload or CDN path
    created_at = Column(DateTime(timezone=True), server_default=func.now(), default=datetime.utcnow, nullable=False)

    # Relationships
    category = relationship("Category", back_populates="posts")
    products = relationship(
        "Product",
        back_populates="post",
        cascade="all, delete-orphan",
        order_by="Product.id"
    )

    def __repr__(self):
        return f"<Post(id={self.id}, title='{self.title}', category_id={self.category_id})>"


class Product(Base):
    """
    Represents individual curated affiliate products featured in 'The Look'.
    Complies with Amazon Associates guidelines by linking directly to Amazon-hosted assets.
    """
    __tablename__ = "products"

    id = Column(Integer, primary_key=True, index=True)
    post_id = Column(Integer, ForeignKey("posts.id", ondelete="CASCADE"), nullable=False)
    asin = Column(String(20), nullable=False, index=True)
    product_title = Column(String(255), nullable=False)
    hook_text = Column(String(500), nullable=False)  # Contextual conversion copy e.g. "Exact matte ceramic vase from photo"
    price = Column(Float, nullable=True)
    amazon_image_url = Column(String(1000), nullable=False)  # MUST remain an Amazon-hosted URL for compliance
    affiliate_url = Column(String(1000), nullable=False)

    # Relationships
    post = relationship("Post", back_populates="products")

    def __repr__(self):
        return f"<Product(id={self.id}, asin='{self.asin}', title='{self.product_title}')>"
