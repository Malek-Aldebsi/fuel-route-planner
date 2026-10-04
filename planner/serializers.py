from rest_framework import serializers

from .services import InputError, resolve_location


class USLocationField(serializers.Field):
    """Validate and normalize supported US location inputs into latitude/longitude coordinates."""
    
    def to_internal_value(self, data):
        try:
            return resolve_location(data, self.field_name)
        except InputError as exc:
            raise serializers.ValidationError(str(exc)) from exc

    def to_representation(self, value):
        return value


class RouteRequestSerializer(serializers.Serializer):
    start = USLocationField()
    finish = USLocationField()

    def validate(self, attrs):
        start, finish = attrs["start"], attrs["finish"]
        if (start["latitude"], start["longitude"]) == (finish["latitude"], finish["longitude"]):
            raise serializers.ValidationError({"finish": "start and finish must be different locations"})
        return attrs
