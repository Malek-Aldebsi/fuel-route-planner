from django.shortcuts import render
from django.views.decorators.http import require_http_methods
from rest_framework import status
from rest_framework.parsers import JSONParser
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from rest_framework.views import APIView

from .serializers import RouteRequestSerializer
from .services import NoFuelPlan, RoutingError, make_plan


class RoutePlanView(APIView):
    parser_classes = [JSONParser]
    renderer_classes = [JSONRenderer]
    authentication_classes = []

    def handle_exception(self, exc):
        response = super().handle_exception(exc)
        if isinstance(response.data, dict) and "detail" in response.data:
            response.data = {"error": str(response.data["detail"])}
        return response

    def post(self, request):
        serializer = RouteRequestSerializer(data=request.data)
        if not serializer.is_valid():
            errors = serializer.errors
            first_error = next(iter(errors.values()))[0]
            return Response({"error": str(first_error), "details": errors}, status=status.HTTP_400_BAD_REQUEST)
        try:
            plan = make_plan(**serializer.validated_data)
        except NoFuelPlan as exc:
            return Response({"error": str(exc)}, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        except RoutingError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response(plan)


@require_http_methods(["GET"])
def map_page(request):
    return render(request, "planner/map.html")
