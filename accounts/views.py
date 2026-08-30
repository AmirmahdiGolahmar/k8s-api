from django.contrib.auth import authenticate, get_user_model, login, logout
from django.core.exceptions import ValidationError as DjangoValidationError
from django.contrib.auth.password_validation import validate_password
from django.middleware.csrf import get_token
from drf_spectacular.utils import OpenApiResponse, extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAdminUser, IsAuthenticated
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


@extend_schema(
    request=inline_serializer('RegisterRequest', fields={
        'username': serializers.CharField(),
        'password': serializers.CharField(),
    }),
    responses={
        201: inline_serializer('RegisterResponse', fields=_user_fields),
        400: OpenApiResponse(description='Username taken, or password does not meet requirements.'),
    },
)
@api_view(['POST'])
@permission_classes([AllowAny])
def register_view(request):
    User = get_user_model()
    username = (request.data.get('username') or '').strip()
    password = request.data.get('password') or ''

    if not username:
        return Response({'detail': 'Username is required.'}, status=status.HTTP_400_BAD_REQUEST)
    if User.objects.filter(username=username).exists():
        return Response({'detail': 'That username is already taken.'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        # Passing an unsaved User(username=...) lets UserAttributeSimilarityValidator
        # (one of AUTH_PASSWORD_VALIDATORS) actually compare the password
        # against the username, same as it would for a real user.
        validate_password(password, user=User(username=username))
    except DjangoValidationError as exc:
        return Response({'detail': ' '.join(exc.messages)}, status=status.HTTP_400_BAD_REQUEST)

    # is_staff/is_superuser are never taken from the request body -- self
    # registration must never be able to grant cluster-management rights
    # (Cluster writes, /docs/, /schema/ are all staff-gated elsewhere).
    user = User.objects.create_user(username=username, password=password, is_staff=False, is_superuser=False)
    login(request, user)
    return Response({'username': user.username, 'is_staff': user.is_staff}, status=status.HTTP_201_CREATED)


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


@extend_schema(
    responses=inline_serializer('UserListItem', fields={
        'id': serializers.IntegerField(),
        'username': serializers.CharField(),
    }, many=True),
)
@api_view(['GET'])
@permission_classes([IsAdminUser])
def list_users(request):
    """Staff-only: every registered user's id + username, so an admin can
    actually pick specific users (A, B, C) for a Cluster/Namespace's
    allowed_users exception list -- there's otherwise no way to know a
    user's id from the dashboard."""
    User = get_user_model()
    return Response(list(User.objects.order_by('username').values('id', 'username')))
