from sqlalchemy import Column, String, Integer, ForeignKey, DateTime, Text, JSON, Table, Boolean, Float
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Index, UniqueConstraint, text
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.database import Base
import uuid

def generate_uuid():
    return str(uuid.uuid4())

class TelemetryEvent(Base):
    __tablename__ = "telemetry_events"
    id = Column(String(36), primary_key=True, default=generate_uuid)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    trace_id = Column(String(32), nullable=False)
    span_id = Column(String(16), nullable=False)
    parent_span_id = Column(String(16), nullable=True)
    request_id = Column(String(36), nullable=True)
    session_id = Column(String(36), nullable=True)
    user_id = Column(String, nullable=True)
    kind = Column(String(32), nullable=False)
    level = Column(String(10), nullable=False)
    name = Column(String(200), nullable=False)
    message = Column(Text, nullable=True)
    duration_ms = Column(Float, nullable=True)
    status_code = Column(Integer, nullable=True)
    fingerprint = Column(String(64), nullable=True)
    attributes = Column(JSON, nullable=False, default=dict)
    environment = Column(String(80), nullable=False)
    release = Column(String(80), nullable=True)
    __table_args__ = (
        Index("ix_telemetry_created", "created_at", "id"),
        Index("ix_telemetry_trace", "trace_id", "created_at"),
        Index("ix_telemetry_session", "session_id", "created_at"),
        Index("ix_telemetry_user", "user_id", "created_at"),
        Index("ix_telemetry_kind", "kind", "created_at"),
        Index("ix_telemetry_errors", "level", "fingerprint", "created_at"),
        Index("ix_telemetry_request", "request_id"),
    )


# Many-to-Many relationship table for User <-> Role
user_roles = Table(
    'user_roles',
    Base.metadata,
    Column('user_id', String, ForeignKey('users.id', ondelete='CASCADE'), primary_key=True),
    Column('role_id', String, ForeignKey('roles.id', ondelete='CASCADE'), primary_key=True),
    Column('created_at', DateTime(timezone=True), server_default=func.now())
)

# Many-to-Many relationship table for Role <-> Permission
role_permissions = Table(
    'role_permissions',
    Base.metadata,
    Column('role_id', String, ForeignKey('roles.id', ondelete='CASCADE'), primary_key=True),
    Column('permission_id', String, ForeignKey('permissions.id', ondelete='CASCADE'), primary_key=True),
    Column('created_at', DateTime(timezone=True), server_default=func.now())
)

# Many-to-Many relationship table for Brand <-> CustomerProfile
brand_profiles = Table(
    'brand_profiles',
    Base.metadata,
    Column('brand_id', String, ForeignKey('brands.id', ondelete='CASCADE'), primary_key=True),
    Column('profile_id', String, ForeignKey('customer_profiles.id', ondelete='CASCADE'), primary_key=True),
    Column('created_at', DateTime(timezone=True), server_default=func.now())
)

class User(Base):
    __tablename__ = "users"

    id = Column(String, primary_key=True, default=generate_uuid)
    email = Column(String, unique=True, nullable=False, index=True)
    hashed_password = Column(String, nullable=False)
    name = Column(String, nullable=True)
    is_active = Column(Boolean, default=True)
    is_superuser = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    roles = relationship("Role", secondary=user_roles, back_populates="users")
    refresh_tokens = relationship("RefreshToken", back_populates="user", cascade="all, delete-orphan")

    def has_permission(self, permission_name: str) -> bool:
        """Check if user has a specific permission through any of their roles"""
        if self.is_superuser:
            return True
        for role in self.roles:
            for permission in role.permissions:
                if permission.name == permission_name:
                    return True
        return False

    def has_role(self, role_name: str) -> bool:
        """Check if user has a specific role"""
        if self.is_superuser:
            return True
        return any(role.name == role_name for role in self.roles)

class Role(Base):
    __tablename__ = "roles"

    id = Column(String, primary_key=True, default=generate_uuid)
    name = Column(String, unique=True, nullable=False)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    users = relationship("User", secondary=user_roles, back_populates="roles")
    permissions = relationship("Permission", secondary=role_permissions, back_populates="roles")

class Permission(Base):
    __tablename__ = "permissions"

    id = Column(String, primary_key=True, default=generate_uuid)
    name = Column(String, unique=True, nullable=False)  # e.g., "brands:create", "ads:delete"
    description = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    roles = relationship("Role", secondary=role_permissions, back_populates="permissions")

class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id = Column(String, primary_key=True, default=generate_uuid)
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    # SHA-256 hash of the raw token — the raw value only ever lives in the
    # client's storage, so a database read cannot mint a valid session.
    token_hash = Column(String, unique=True, nullable=True, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="refresh_tokens")

class Brand(Base):
    __tablename__ = "brands"

    id = Column(String, primary_key=True, default=generate_uuid)
    name = Column(String, nullable=False)
    logo = Column(String, nullable=True)
    primary_color = Column(String, default='#3B82F6')
    secondary_color = Column(String, default='#10B981')
    highlight_color = Column(String, default='#F59E0B')
    voice = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    products = relationship("Product", back_populates="brand", cascade="all, delete-orphan")
    profiles = relationship("CustomerProfile", secondary=brand_profiles, back_populates="brands")
    generated_ads = relationship("GeneratedAd", back_populates="brand")

    @property
    def colors(self):
        return {
            "primary": self.primary_color,
            "secondary": self.secondary_color,
            "highlight": self.highlight_color
        }

    @property
    def profileIds(self):
        return [p.id for p in self.profiles]

class Product(Base):
    __tablename__ = "products"

    id = Column(String, primary_key=True, default=generate_uuid)
    brand_id = Column(String, ForeignKey("brands.id", ondelete="CASCADE"), nullable=False)
    name = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    product_shots = Column(JSON, nullable=True)
    default_url = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    brand = relationship("Brand", back_populates="products")

class CustomerProfile(Base):
    __tablename__ = "customer_profiles"

    id = Column(String, primary_key=True, default=generate_uuid)
    name = Column(String, nullable=False)
    demographics = Column(Text, nullable=True)
    pain_points = Column(Text, nullable=True)
    goals = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    brands = relationship("Brand", secondary=brand_profiles, back_populates="profiles")

class CampaignPreset(Base):
    __tablename__ = 'campaign_presets'

    id = Column(String, primary_key=True, default=generate_uuid)
    user_id = Column(String, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    ad_account_id = Column(String, nullable=False, index=True)
    name = Column(String, nullable=False)
    vertical = Column(String, nullable=False, default='')
    settings = Column(JSON, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class CampaignPreference(Base):
    __tablename__ = 'campaign_preferences'

    id = Column(String, primary_key=True)
    settings = Column(JSON, nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class LeadRouterConnection(Base):
    __tablename__ = "leadrouter_connections"

    id = Column(String, primary_key=True, default=generate_uuid)
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True)
    account_type = Column(String, nullable=False)
    account_name = Column(String, nullable=False)
    partner_id = Column(String, nullable=True)
    encrypted_api_key = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    __table_args__ = (CheckConstraint("account_type IN ('partner', 'organization')", name="ck_leadrouter_account_type"),)


class LeadRouterDefault(Base):
    __tablename__ = "leadrouter_defaults"

    id = Column(String, primary_key=True, default=generate_uuid)
    connection_id = Column(String, ForeignKey("leadrouter_connections.id", ondelete="CASCADE"), nullable=False)
    resource_type = Column(String, nullable=False)
    resource_id = Column(String, nullable=False)
    campaign_id = Column(String, nullable=False)
    campaign = Column(JSON, nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    __table_args__ = (
        UniqueConstraint("connection_id", "resource_type", "resource_id", name="uq_leadrouter_default"),
        CheckConstraint("resource_type IN ('brand', 'product', 'campaign')", name="ck_leadrouter_resource_type"),
    )


class FacebookCampaign(Base):
    __tablename__ = "facebook_campaigns"

    id = Column(String, primary_key=True, default=generate_uuid)
    created_by_id = Column(String, ForeignKey("users.id", ondelete="SET NULL"))
    name = Column(String, nullable=False)
    objective = Column(String, nullable=False)
    budget_type = Column(String, nullable=False)
    daily_budget = Column(Integer, nullable=True)
    daily_budget_minor = Column(Integer, nullable=True)
    bid_strategy = Column(String, nullable=True)
    status = Column(String, default='PAUSED')
    fb_campaign_id = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    adsets = relationship("FacebookAdSet", back_populates="campaign", cascade="all, delete-orphan")

class FacebookAdSet(Base):
    __tablename__ = "facebook_adsets"

    id = Column(String, primary_key=True, default=generate_uuid)
    created_by_id = Column(String, ForeignKey("users.id", ondelete="SET NULL"))
    campaign_id = Column(String, ForeignKey("facebook_campaigns.id", ondelete="CASCADE"), nullable=False)
    name = Column(String, nullable=False)
    optimization_goal = Column(String, nullable=False)
    daily_budget = Column(Integer, nullable=True)
    daily_budget_minor = Column(Integer, nullable=True)
    bid_strategy = Column(String, nullable=True)
    bid_amount = Column(Integer, nullable=True)
    bid_amount_minor = Column(Integer, nullable=True)
    targeting = Column(JSON, nullable=True)
    pixel_id = Column(String, nullable=True)
    conversion_event = Column(String, nullable=True)
    status = Column(String, default='PAUSED')
    fb_adset_id = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    campaign = relationship("FacebookCampaign", back_populates="adsets")
    ads = relationship("FacebookAd", back_populates="adset", cascade="all, delete-orphan")

class FacebookAd(Base):
    __tablename__ = "facebook_ads"

    id = Column(String, primary_key=True, default=generate_uuid)
    created_by_id = Column(String, ForeignKey("users.id", ondelete="SET NULL"))
    adset_id = Column(String, ForeignKey("facebook_adsets.id", ondelete="CASCADE"), nullable=False)
    name = Column(String, nullable=False)
    creative_name = Column(String, nullable=True)
    image_url = Column(String, nullable=True)
    # Video support fields
    media_type = Column(String, default='image')  # 'image' or 'video'
    video_url = Column(String, nullable=True)
    video_id = Column(String, nullable=True)  # Facebook video ID
    thumbnail_url = Column(String, nullable=True)
    bodies = Column(JSON, nullable=True)
    headlines = Column(JSON, nullable=True)
    description = Column(Text, nullable=True)
    cta = Column(String, nullable=True)
    website_url = Column(String, nullable=True)
    status = Column(String, default='PAUSED')
    fb_ad_id = Column(String, nullable=True)
    fb_creative_id = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    adset = relationship("FacebookAdSet", back_populates="ads")

class WinningAd(Base):
    __tablename__ = "winning_ads"

    id = Column(String, primary_key=True, default=generate_uuid)
    name = Column(String, nullable=False)
    image_url = Column(String, nullable=False)
    notes = Column(Text, nullable=True)
    tags = Column(Text, nullable=True)
    analysis = Column(Text, nullable=True)
    recreation_prompt = Column(Text, nullable=True)
    topic = Column(String, nullable=True)
    mood = Column(String, nullable=True)
    subject_matter = Column(String, nullable=True)
    copy_analysis = Column(Text, nullable=True)
    product_name = Column(String, nullable=True)
    category = Column(String, nullable=True)
    design_style = Column(String, nullable=True)
    filename = Column(String, nullable=True)
    structural_analysis = Column(Text, nullable=True)
    layering = Column(Text, nullable=True)
    template_structure = Column(JSON, nullable=True)
    color_palette = Column(JSON, nullable=True)
    typography_system = Column(JSON, nullable=True)
    copy_patterns = Column(JSON, nullable=True)
    visual_elements = Column(JSON, nullable=True)
    template_category = Column(String, nullable=True)

    # Ad Remix Engine fields
    blueprint_json = Column(JSON, nullable=True)  # Stores the deconstructed blueprint
    blueprint_analyzed_at = Column(DateTime(timezone=True), nullable=True)  # When blueprint was created

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    generated_ads = relationship("GeneratedAd", back_populates="template")

class GeneratedAd(Base):
    __tablename__ = "generated_ads"

    id = Column(String, primary_key=True, default=generate_uuid)
    generation_context = Column(JSON, nullable=True)
    created_by_id = Column(String, ForeignKey("users.id", ondelete="SET NULL"))
    brand_id = Column(String, ForeignKey("brands.id", ondelete="SET NULL"), nullable=True)
    product_id = Column(String, ForeignKey("products.id", ondelete="SET NULL"), nullable=True) # Assuming product_id is also FK, though not explicit in original schema it makes sense
    template_id = Column(String, ForeignKey("winning_ads.id", ondelete="SET NULL"), nullable=True)
    image_url = Column(String, nullable=True)  # Changed to nullable for video ads
    headline = Column(String, nullable=True)
    body = Column(Text, nullable=True)
    cta = Column(String, nullable=True)
    size_name = Column(String, nullable=True)
    dimensions = Column(String, nullable=True)
    prompt = Column(Text, nullable=True)
    ad_bundle_id = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    # Video support fields
    media_type = Column(String, default='image')  # 'image' or 'video'
    video_url = Column(String, nullable=True)
    video_id = Column(String, nullable=True)  # Facebook video ID
    thumbnail_url = Column(String, nullable=True)

    brand = relationship("Brand", back_populates="generated_ads")
    template = relationship("WinningAd", back_populates="generated_ads")

class Vertical(Base):
    __tablename__ = "verticals"

    id = Column(String, primary_key=True, default=generate_uuid)
    name = Column(String, nullable=False, unique=True, index=True)  # e.g., "Legal", "Fitness", "E-commerce"
    description = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    saved_searches = relationship("SavedSearch", back_populates="vertical")


class FacebookPage(Base):
    __tablename__ = "facebook_pages"

    id = Column(String, primary_key=True, default=generate_uuid)
    page_name = Column(String, nullable=False, unique=True, index=True)
    page_url = Column(String, nullable=True)
    vertical_id = Column(String, ForeignKey('verticals.id', ondelete='SET NULL'), nullable=True)
    total_ads = Column(Integer, default=0)  # Cached count of ads from this page
    first_seen = Column(DateTime(timezone=True), server_default=func.now())
    last_seen = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    vertical = relationship("Vertical")
    ads = relationship("ScrapedAd", back_populates="facebook_page")


class SavedSearch(Base):
    __tablename__ = "saved_searches"

    id = Column(String, primary_key=True, default=generate_uuid)
    query = Column(String, nullable=False)
    country = Column(String, nullable=True)
    negative_keywords = Column(JSON, nullable=True)  # List of negative keywords
    vertical_id = Column(String, ForeignKey('verticals.id', ondelete='SET NULL'), nullable=True)
    search_type = Column(String, default='one_time')  # 'one_time', 'scheduled_daily', 'scheduled_weekly'
    schedule_config = Column(JSON, nullable=True)  # Cron schedule config for scheduled searches
    is_active = Column(Boolean, default=True)  # For scheduled searches
    last_run = Column(DateTime(timezone=True), nullable=True)
    ads_requested = Column(Integer, nullable=True)  # How many ads were requested (limit)
    ads_returned = Column(Integer, nullable=True)  # How many ads API returned
    ads_new = Column(Integer, nullable=True)  # How many new ads (not duplicates)
    ads_duplicate = Column(Integer, nullable=True)  # How many duplicate ads
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    vertical = relationship("Vertical", back_populates="saved_searches")
    ads = relationship("ScrapedAd", back_populates="saved_search", cascade="all, delete-orphan")


class ApiUsageLog(Base):
    __tablename__ = "api_usage_logs"

    id = Column(String, primary_key=True, default=generate_uuid)
    endpoint = Column(String, nullable=False)  # "facebook_ads_library"
    api_calls = Column(Integer, nullable=False)  # Number of API calls made
    ads_returned = Column(Integer, nullable=False)  # Ads returned from API
    ads_saved = Column(Integer, nullable=False)  # Ads saved after filtering
    query = Column(String, nullable=True)  # Search query
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    date = Column(String, nullable=False, index=True)  # YYYY-MM-DD for daily grouping


class PageBlacklist(Base):
    __tablename__ = "page_blacklist"

    id = Column(String, primary_key=True, default=generate_uuid)
    page_name = Column(String, nullable=False, unique=True, index=True)  # Facebook page name
    reason = Column(String, nullable=True)  # Optional reason for blacklisting
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class KeywordBlacklist(Base):
    __tablename__ = "keyword_blacklist"

    id = Column(String, primary_key=True, default=generate_uuid)
    keyword = Column(String, nullable=False, unique=True, index=True)  # Keyword to filter
    reason = Column(String, nullable=True)  # Optional reason for blacklisting
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class SearchLog(Base):
    __tablename__ = "search_logs"

    id = Column(String, primary_key=True, default=generate_uuid)
    search_query = Column(String, nullable=False)
    country = Column(String, nullable=True)
    negative_keywords = Column(JSON, nullable=True)  # List of keywords excluded
    vertical_id = Column(String, ForeignKey('verticals.id', ondelete='SET NULL'), nullable=True)

    # Metrics
    total_ads_found = Column(Integer, default=0)  # Total ads returned from API
    filtered_by_page_blacklist = Column(Integer, default=0)  # Ads filtered by page blacklist
    filtered_by_keyword_blacklist = Column(Integer, default=0)  # Ads filtered by keyword blacklist
    final_ads_saved = Column(Integer, default=0)  # Final count after all filtering

    # New pages discovered
    new_pages_blacklisted = Column(JSON, nullable=True)  # List of page names added to blacklist during/after search

    # Execution details
    api_calls_made = Column(Integer, default=0)
    search_type = Column(String, nullable=True)  # 'one_time', 'scheduled_daily', 'scheduled_weekly'
    execution_time_seconds = Column(Integer, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    date = Column(String, nullable=False, index=True)  # YYYY-MM-DD for daily grouping

    vertical = relationship("Vertical")


class ScrapedAd(Base):
    __tablename__ = "scraped_ads"

    id = Column(String, primary_key=True, default=generate_uuid)
    brand_name = Column(String, nullable=True)  # DEPRECATED: Use facebook_page relationship instead
    headline = Column(String, nullable=True)  # Ad headline
    ad_copy = Column(Text, nullable=True)  # Ad body text
    cta_text = Column(String, nullable=True)
    platform = Column(String, default='facebook')
    external_id = Column(String, nullable=True, unique=True, index=True)  # ID from platform
    content_hash = Column(String, nullable=True, unique=True, index=True)  # Hash of ad content for deduplication
    ad_link = Column(String, nullable=False)  # Link to original ad on FB Ads Library
    platforms = Column(JSON, nullable=True)  # ['facebook', 'instagram'] etc
    start_date = Column(String, nullable=True)  # When ad started running
    media_type = Column(String, nullable=True)  # 'image', 'video', or 'carousel'
    first_seen = Column(DateTime(timezone=True), server_default=func.now())  # First time ad was scraped
    last_seen = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())  # Last time ad was seen
    seen_count = Column(Integer, default=1)  # Number of times this ad has been encountered in scrapes
    search_id = Column(String, ForeignKey('saved_searches.id', ondelete='CASCADE'), nullable=True)  # Link to search
    facebook_page_id = Column(String, ForeignKey('facebook_pages.id', ondelete='SET NULL'), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    saved_search = relationship("SavedSearch", back_populates="ads")
    facebook_page = relationship("FacebookPage", back_populates="ads")

class Prompt(Base):
    __tablename__ = "prompts"

    id = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    category = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    variables = Column(JSON, nullable=True)  # List of variable names
    template = Column(Text, nullable=False)  # The actual prompt template
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

class AdStyle(Base):
    __tablename__ = "ad_styles"

    id = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    category = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    best_for = Column(JSON, nullable=True)  # List of industries
    visual_layout = Column(String, nullable=True)
    psychology = Column(Text, nullable=True)
    mood = Column(String, nullable=True)
    lighting = Column(String, nullable=True)
    composition = Column(String, nullable=True)
    design_style = Column(String, nullable=True)
    prompt = Column(Text, nullable=True)  # Image generation prompt
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


class BrandScrape(Base):
    """Tracks scraping sessions for a specific Facebook page/brand."""
    __tablename__ = "brand_scrapes"

    id = Column(String, primary_key=True, default=generate_uuid)
    brand_name = Column(String, nullable=False, index=True)  # User-defined name, also R2 folder name
    page_id = Column(String, nullable=False)  # FB page ID from URL
    page_name = Column(String, nullable=True)  # Actual FB page name (discovered during scrape)
    page_url = Column(String, nullable=False)  # Original FB Ads Library URL
    total_ads = Column(Integer, default=0)  # Total ads found
    media_downloaded = Column(Integer, default=0)  # Successfully downloaded media count
    status = Column(String, default='pending')  # pending, scraping, completed, failed
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    ads = relationship("BrandScrapedAd", back_populates="brand_scrape", cascade="all, delete-orphan")


class BrandScrapedAd(Base):
    """Individual ad scraped from a brand's Facebook page with media stored on R2."""
    __tablename__ = "brand_scraped_ads"

    id = Column(String, primary_key=True, default=generate_uuid)
    brand_scrape_id = Column(String, ForeignKey('brand_scrapes.id', ondelete='CASCADE'), nullable=False)
    external_id = Column(String, nullable=False, index=True)  # FB ad library ID
    page_name = Column(String, nullable=True)  # Facebook page name
    page_link = Column(String, nullable=True)  # Link to page's ads in library
    headline = Column(String, nullable=True)
    ad_copy = Column(Text, nullable=True)
    cta_text = Column(String, nullable=True)
    media_type = Column(String, nullable=True)  # image, video, carousel
    media_urls = Column(JSON, nullable=True)  # R2 URLs for downloaded media
    original_media_urls = Column(JSON, nullable=True)  # Original FB media URLs
    platforms = Column(JSON, nullable=True)  # ['facebook', 'instagram']
    start_date = Column(String, nullable=True)
    ad_link = Column(String, nullable=True)  # FB Ads Library link
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    brand_scrape = relationship("BrandScrape", back_populates="ads")


class PluginInstallation(Base):
    __tablename__ = "plugin_installations"
    id = Column(String, primary_key=True, default=generate_uuid)
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    slug = Column(String, nullable=False)
    version = Column(String, nullable=False)
    document = Column(JSON, nullable=False)
    package_digest = Column(String, nullable=False)
    enabled = Column(Boolean, nullable=False, default=True, server_default="true")
    configuration = Column(JSON, nullable=False, default=dict)
    worker_key_hash = Column(String, nullable=True, unique=True)
    worker_key_prefix = Column(String, nullable=True)
    worker_key_expires_at = Column(DateTime(timezone=True), nullable=True)
    worker_last_seen_at = Column(DateTime(timezone=True), nullable=True)
    worker_generation = Column(Integer, nullable=False, default=0, server_default="0")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    archived_at = Column(DateTime(timezone=True), nullable=True)
    __table_args__ = (UniqueConstraint("user_id", "slug", "version", name="uq_plugin_release"),)


class PluginRun(Base):
    __tablename__ = "plugin_runs"
    id = Column(String, primary_key=True, default=generate_uuid)
    installation_id = Column(String, ForeignKey("plugin_installations.id", ondelete="CASCADE"), nullable=False, index=True)
    request_id = Column(String, nullable=False)
    input_digest = Column(String, nullable=False)
    package_digest = Column(String, nullable=False)
    inputs = Column(JSON, nullable=False)
    configuration = Column(JSON, nullable=False)
    status = Column(String, nullable=False)
    output = Column(JSON, nullable=True)
    error = Column(String, nullable=True)
    lease_hash = Column(String, nullable=True)
    lease_expires_at = Column(DateTime(timezone=True), nullable=True)
    worker_generation = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        UniqueConstraint("installation_id", "request_id", name="uq_plugin_run_request"),
        CheckConstraint("status IN ('queued','running','succeeded','failed','cancelled','expired')", name="ck_plugin_run_status"),
    )


class ApiKey(Base):
    """Machine-to-machine key (e.g. the Hermes Telegram bot). Hashed at rest —
    the plaintext key is shown once at creation time and never stored or logged."""
    __tablename__ = "api_keys"

    id = Column(String, primary_key=True, default=generate_uuid)
    name = Column(String, nullable=False)
    key_hash = Column(String, unique=True, nullable=False, index=True)
    # Platform keys and legacy bot keys use distinct, enforced scope sets.
    scopes = Column(JSON, nullable=False, default=list)
    created_by_user_id = Column(String, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    last_used_at = Column(DateTime(timezone=True), nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)

    key_prefix = Column(String(20), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)

    created_by = relationship("User")


class GoogleAdsConnection(Base):
    """One connected Google Ads account, tokens encrypted at rest via Fernet
    (see app.core.token_encryption)."""
    __tablename__ = "google_ads_connections"

    id = Column(String, primary_key=True, default=generate_uuid)
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    customer_id = Column(String, nullable=False)  # Google Ads customer ID (no dashes)
    account_name = Column(String, nullable=True)
    encrypted_refresh_token = Column(Text, nullable=False)
    encrypted_access_token = Column(Text, nullable=True)
    access_token_expires_at = Column(DateTime(timezone=True), nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    user = relationship("User")


class MetaAdsConnection(Base):
    """One Meta Ads account authorized by a user OAuth grant."""
    __tablename__ = "meta_ads_connections"

    id = Column(String, primary_key=True, default=generate_uuid)
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    ad_account_id = Column(String, nullable=False)
    account_name = Column(String, nullable=True)
    encrypted_access_token = Column(Text, nullable=False)
    access_token_expires_at = Column(DateTime(timezone=True), nullable=True)
    is_active = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    user = relationship("User")


class TikTokAdsConnection(Base):
    """One TikTok Marketing API advertiser connection, with OAuth tokens
    encrypted at rest through app.core.token_encryption."""
    __tablename__ = "tiktok_ads_connections"

    id = Column(String, primary_key=True, default=generate_uuid)
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    advertiser_id = Column(String, nullable=False)
    account_name = Column(String, nullable=True)
    encrypted_refresh_token = Column(Text, nullable=False)
    encrypted_access_token = Column(Text, nullable=True)
    access_token_expires_at = Column(DateTime(timezone=True), nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    user = relationship("User")


class Workspace(Base):
    __tablename__ = "workspaces"
    id = Column(String, primary_key=True, default=generate_uuid)
    name = Column(String(120), nullable=False)
    is_active = Column(Boolean, nullable=False, server_default="true")
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class WorkspaceMembership(Base):
    __tablename__ = "workspace_memberships"
    workspace_id = Column(
        String, ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    user_id = Column(
        String, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    role = Column(String(32), nullable=False)
    is_active = Column(Boolean, nullable=False, server_default="true")
    version = Column(Integer, nullable=False, server_default="1")
    __table_args__ = (
        CheckConstraint(
            "role IN ('viewer','creative_editor','buyer','publisher','admin')",
            name="ck_workspace_member_role",
        ),
        CheckConstraint("version > 0", name="ck_workspace_member_version"),
    )


class WorkspaceAccount(Base):
    __tablename__ = "workspace_accounts"
    id = Column(String, primary_key=True, default=generate_uuid)
    workspace_id = Column(
        String, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    provider = Column(String(20), nullable=False, server_default="meta")
    external_account_id = Column(String(100), nullable=False)
    meta_connection_id = Column(
        String,
        ForeignKey("meta_ads_connections.id", ondelete="SET NULL"),
        nullable=True,
    )
    is_active = Column(Boolean, nullable=False, server_default="true")
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_workspace_account_scope"),
        UniqueConstraint(
            "workspace_id",
            "provider",
            "external_account_id",
            name="uq_workspace_provider_account",
        ),
        CheckConstraint("provider = 'meta'", name="ck_workspace_account_provider"),
    )


class WorkspaceAccountGrant(Base):
    __tablename__ = "workspace_account_grants"
    workspace_id = Column(String, primary_key=True)
    account_id = Column(String, primary_key=True)
    user_id = Column(String, primary_key=True)
    can_sync = Column(Boolean, nullable=False, server_default="false")
    is_active = Column(Boolean, nullable=False, server_default="true")
    version = Column(Integer, nullable=False, server_default="1")
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "account_id"],
            ["workspace_accounts.workspace_id", "workspace_accounts.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "user_id"],
            ["workspace_memberships.workspace_id", "workspace_memberships.user_id"],
            ondelete="CASCADE",
        ),
        CheckConstraint("version > 0", name="ck_workspace_grant_version"),
    )


class AccountSyncJob(Base):
    __tablename__ = "account_sync_jobs"
    id = Column(String, primary_key=True, default=generate_uuid)
    workspace_id = Column(String, nullable=False)
    account_id = Column(String, nullable=False)
    resource = Column(String(32), nullable=False, server_default="campaigns")
    requested_by_user_id = Column(
        String, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    membership_version = Column(Integer, nullable=False)
    grant_version = Column(Integer, nullable=False)
    connection_id = Column(
        String,
        ForeignKey("meta_ads_connections.id", ondelete="SET NULL"),
        nullable=True,
    )
    credential_owner_version = Column(Integer, nullable=False)
    available_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    status = Column(String(20), nullable=False, server_default="queued")
    attempts = Column(Integer, nullable=False, server_default="0")
    pages_fetched = Column(Integer, nullable=False, server_default="0")
    worker_id = Column(String(120), nullable=True)
    lease_token = Column(String, nullable=True)
    lease_expires_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    error_code = Column(String(40), nullable=True)
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "account_id"],
            ["workspace_accounts.workspace_id", "workspace_accounts.id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "workspace_id", "account_id", "id", name="uq_account_job_scope"
        ),
        CheckConstraint("resource = 'campaigns'", name="ck_account_job_resource"),
        CheckConstraint(
            "status IN ('queued','running','succeeded','failed','blocked')",
            name="ck_account_job_status",
        ),
        CheckConstraint(
            "attempts >= 0 AND pages_fetched >= 0", name="ck_account_job_counts"
        ),
        CheckConstraint(
            "(status = 'running' AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL) OR (status <> 'running' AND lease_token IS NULL AND lease_expires_at IS NULL)",
            name="ck_account_job_lease",
        ),
        Index(
            "uq_active_account_sync",
            "workspace_id",
            "account_id",
            "resource",
            unique=True,
            postgresql_where=text("status IN ('queued','running')"),
        ),
        Index("ix_account_sync_claim", "status", "created_at"),
    )


class AccountSnapshot(Base):
    __tablename__ = "account_snapshots"
    workspace_id = Column(String, primary_key=True)
    account_id = Column(String, primary_key=True)
    resource = Column(String(32), primary_key=True)
    generation_id = Column(String, nullable=False)
    items = Column(JSON, nullable=False)
    last_success_at = Column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "account_id"],
            ["workspace_accounts.workspace_id", "workspace_accounts.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "account_id", "generation_id"],
            [
                "account_sync_jobs.workspace_id",
                "account_sync_jobs.account_id",
                "account_sync_jobs.id",
            ],
            ondelete="CASCADE",
        ),
        CheckConstraint("resource = 'campaigns'", name="ck_account_snapshot_resource"),
    )


class WorkspaceAuditEvent(Base):
    __tablename__ = "workspace_audit_events"
    id = Column(String, primary_key=True, default=generate_uuid)
    workspace_id = Column(
        String,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    actor_user_id = Column(
        String, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    action = Column(String(64), nullable=False)
    resource_id = Column(String, nullable=False)
    details = Column(JSON, nullable=False)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class UserTheme(Base):
    __tablename__ = "user_themes"
    id = Column(String, primary_key=True, default=generate_uuid)
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    document = Column(JSON, nullable=False)
    github_url = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class InstallationState(Base):
    __tablename__ = "installation_state"
    id = Column(Integer, primary_key=True)
    installation_id = Column(String, nullable=False, unique=True, default=generate_uuid)
    initialized = Column(Boolean, nullable=False, default=False)
    setup_status = Column(String(20), nullable=False, default="pending")
    setup_step = Column(String(20), nullable=False, default="welcome")
    worker_heartbeat_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_installation_singleton"),
        CheckConstraint("setup_status IN ('pending','in_progress','deferred','complete')", name="ck_installation_status"),
        CheckConstraint("setup_step IN ('welcome','providers','brand','create')", name="ck_installation_step"),
    )


class ProviderConnection(Base):
    __tablename__ = "provider_connections"
    provider = Column(String(20), primary_key=True)
    encrypted_key = Column(Text, nullable=True)
    key_hint = Column(String(4), nullable=True)
    disabled = Column(Boolean, nullable=False, default=False)
    version = Column(Integer, nullable=False, default=1)
    status = Column(String(32), nullable=False, default="saved_unverified")
    status_message = Column(String(300), nullable=True)
    last_checked_at = Column(DateTime(timezone=True), nullable=True)
    updated_by_user_id = Column(String, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    __table_args__ = (
        CheckConstraint("provider IN ('gemini','fal','kie')", name="ck_provider_name"),
        CheckConstraint("status IN ('not_configured','saved_unverified','connected','invalid','insufficient_credit','temporarily_unavailable')", name="ck_provider_status"),
    )


class FeedbackSubmission(Base):
    """A durable copy of in-app feedback, mirrored to a private project board.

    Telemetry events are pruned by age and volume; product feedback must not be,
    so the submission is stored here in the same transaction as its telemetry row.
    """

    __tablename__ = "feedback_submissions"
    id = Column(String(36), primary_key=True, default=generate_uuid)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    event_id = Column(String(36), unique=True)
    user_id = Column(String(36), ForeignKey("users.id", ondelete="SET NULL"))
    category = Column(String(16), nullable=False, server_default="bug")
    page = Column(String(500))
    message = Column(Text, nullable=False)
    trace_id = Column(String(32))
    status = Column(String(16), nullable=False, server_default="pending")
    attempts = Column(Integer, nullable=False, server_default="0")
    next_attempt_at = Column(DateTime(timezone=True))
    last_error = Column(Text)
    ticket_id = Column(String(64))
    sent_at = Column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint("status IN ('pending','sent','failed')", name="ck_feedback_status"),
        Index("ix_feedback_submissions_pending", "status", "next_attempt_at"),
    )
