from django.contrib.auth import authenticate, login, logout
from django.middleware.csrf import get_token
from drf_spectacular.utils import OpenApiResponse, extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

_user_fields = {
    'username': serializers.CharField(),
    'is_staff': serializers.BooleanField(),
}


@extend_schema(responses=inline_serializer('CsrfResponse', fields={'csrfToken': serializers.CharField()}))
@api_view(['GET'])
@permission_classes([AllowAny])
def csrf(request):
    """Forces Django to set the csrftoken cookie -- it otherwise only does
    so the first time something reads the token, which never happens on a
    pure JSON API with no rendered forms. The frontend calls this once,
    before its first POST (login), then reads the cookie itself and sends
    it back as the X-CSRFToken header on every unsafe request."""
    return Response({'csrfToken': get_token(request)})


@extend_schema(
    request=inline_serializer('LoginRequest', fields={
        'username': serializers.CharField(),
        'password': serializers.CharField(),
    }),
    responses={
        200: inline_serializer('LoginResponse', fields=_user_fields),
        401: OpenApiResponse(description='Invalid credentials.'),
    },
)
@api_view(['POST'])
@permission_classes([AllowAny])
def login_view(request):
    username = request.data.get('username')
    password = request.data.get('password')
    user = authenticate(request, username=username, password=password)
    if user is None:
        return Response({'detail': 'Invalid credentials.'}, status=status.HTTP_401_UNAUTHORIZED)
    login(request, user)
    return Response({'username': user.username, 'is_staff': user.is_staff})


@extend_schema(request=None, responses={204: None})
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def logout_view(request):
    logout(request)
    return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(responses=inline_serializer('CurrentUserResponse', fields=_user_fields))
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def me(request):
    return Response({'username': request.user.username, 'is_staff': request.user.is_staff})
