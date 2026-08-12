@echo off
setlocal
cd /d "%~dp0backend"
echo Microsoft D365 connection setup
echo Do not share the Client Secret in chat or email.
set /p D365_TENANT_ID=Tenant ID: 
set /p D365_CLIENT_ID=Client/Application ID: 
set /p D365_CLIENT_SECRET=Client Secret: 
set /p D365_RESOURCE_URL=D365 URL (https://company.operations.dynamics.com): 
set /p D365_COMPANY=Legal entity/company code: 
(
echo D365_TENANT_ID=%D365_TENANT_ID%
echo D365_CLIENT_ID=%D365_CLIENT_ID%
echo D365_CLIENT_SECRET=%D365_CLIENT_SECRET%
echo D365_RESOURCE_URL=%D365_RESOURCE_URL%
echo D365_COMPANY=%D365_COMPANY%
echo D365_ENABLE_WRITEBACK=false
) > .env
echo.
echo Configuration saved. Restart the backend, then test D365 in the Integration Hub.
pause
endlocal
