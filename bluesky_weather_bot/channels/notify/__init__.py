"""Notification channels — output destinations for weather responses."""
from bluesky_weather_bot.channels.notify.base import NotificationChannel, NotificationPayload, NotificationResult
from bluesky_weather_bot.channels.notify.bluesky_post import BlueskyPostNotifyChannel
from bluesky_weather_bot.channels.notify.bluesky_dm import BlueskyDMNotifyChannel

__all__ = [
    "NotificationChannel", "NotificationPayload", "NotificationResult",
    "BlueskyPostNotifyChannel", "BlueskyDMNotifyChannel",
]
